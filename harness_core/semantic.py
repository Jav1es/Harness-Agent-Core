"""The semantic layer (L5): checks that *must* call a model, kept separate.

Why a separate layer
--------------------
Everything in :mod:`harness_core.guards` is deterministic: same input, same verdict,
no cost, no network. That is what makes it trustworthy — a JSON Schema failure is a
fact, not an opinion.

Some requirements cannot be checked that way. "Does this summary actually cite the
retrieved documents?" needs judgement. Putting such a check into the deterministic
validator would poison the whole layer: a flaky judge would make green runs red and
red runs green, and nobody could tell which layer said what.

So this module is deliberately a *different* layer with different rules:

* **It costs money.** Every check records the tokens it spent, and the total is
  reported alongside the run.
* **It can be wrong.** A judge's verdict is evidence, not proof. It is reported as
  a finding with its own gate name so it is never confused with a schema failure.
* **It has a hard budget.** ``max_checks`` caps how many judgements a run may buy.
  When the budget runs out the gate reports BLOCK (not "skip"), because silently
  skipping a check turns "unverified" into "passed".

Why budget exhaustion blocks
----------------------------
A cheaper default would be to let the run pass when the judge budget runs out. That
is the one behaviour a CI gate must not have: it converts "we could not afford to
verify this" into "this is verified". Blocking is the honest answer, and the failure
message says exactly which limit was hit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable

from .adapters.llm import LLMAdapter
from .errors import ERROR_SEMANTIC, Finding, Severity
from .state import AgentHarnessState

#: Default cap on how many model-backed judgements a single run may buy.
DEFAULT_MAX_CHECKS = 8


class SemanticBudgetExhausted(RuntimeError):
    """Raised internally when a gate would exceed its check budget."""


@dataclass
class SemanticVerdict:
    """One judgement: did the payload satisfy a natural-language requirement?"""

    ok: bool
    reason: str = ""
    tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "reason": self.reason, "tokens": self.tokens}


@runtime_checkable
class SemanticGate(Protocol):
    """A check that needs a model to decide.

    Implementations receive the payload plus a rendered context (the requirement in
    words) and return a :class:`SemanticVerdict`.
    """

    name: str

    def evaluate(self, payload: Any, requirement: str) -> SemanticVerdict: ...


class LLMJudgeGate:
    """Asks a model to answer a yes/no question about the payload.

    The prompt is intentionally narrow: a judge that is asked to "review" will
    invent criteria, and invented criteria are indistinguishable from real findings.
    """

    name = "llm_judge"

    def __init__(
        self,
        llm: LLMAdapter,
        *,
        system_prompt: str | None = None,
        max_payload_chars: int = 8000,
    ) -> None:
        self.llm = llm
        self.system_prompt = system_prompt or (
            "You are a strict reviewer. Answer with JSON only: "
            '{"ok": true|false, "reason": "<one short sentence>"}. '
            "Answer ok=false when the requirement is not clearly satisfied. "
            "Never invent requirements that were not asked about."
        )
        self.max_payload_chars = max_payload_chars

    def evaluate(self, payload: Any, requirement: str) -> SemanticVerdict:
        rendered = json.dumps(payload, ensure_ascii=False, default=str)
        if len(rendered) > self.max_payload_chars:
            rendered = rendered[: self.max_payload_chars] + "...<truncated>"
        prompt = (
            f"{self.system_prompt}\n\n"
            f"REQUIREMENT:\n{requirement}\n\n"
            f"PAYLOAD:\n{rendered}\n\n"
            "Answer with the JSON object only."
        )
        state = AgentHarnessState(task_prompt="semantic-gate", tool_whitelist=[])
        response = self.llm.generate(prompt, state)
        tokens = int(response.tokens_estimate or 0)

        if response.error:
            return SemanticVerdict(
                ok=False,
                reason=f"judge failed: {response.error}",
                tokens=tokens,
            )
        verdict = _parse_verdict(response.content, response.raw_text)
        if verdict is None:
            return SemanticVerdict(
                ok=False,
                reason=f"judge returned an unparseable answer: {response.raw_text[:200]!r}",
                tokens=tokens,
            )
        return SemanticVerdict(ok=verdict[0], reason=verdict[1], tokens=tokens)


def _parse_verdict(content: Any, raw_text: str) -> tuple[bool, str] | None:
    """Accept ``{"ok": bool, "reason": str}`` from content or raw text.

    An unparseable answer becomes a failed verdict, never a pass: a judge that
    cannot follow its own output contract is not evidence that the payload is good.
    """
    candidates: list[Any] = []
    if isinstance(content, dict):
        candidates.append(content)
    if isinstance(content, str) and content.strip():
        candidates.append(content)
    if raw_text.strip():
        candidates.append(raw_text)

    for candidate in candidates:
        data = candidate
        if isinstance(candidate, str):
            text = candidate.strip()
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end <= start:
                continue
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
        if not isinstance(data, dict) or "ok" not in data:
            continue
        ok = data.get("ok")
        if isinstance(ok, str):
            ok = ok.strip().lower() in ("true", "yes", "1")
        return bool(ok), str(data.get("reason", ""))
    return None


@dataclass
class SemanticRequirement:
    """A natural-language requirement plus the gate that judges it."""

    name: str
    requirement: str
    gate: SemanticGate
    severity: Severity = "BLOCK"


@dataclass
class SemanticLayer:
    """Runs the model-backed checks and accounts for what they cost.

    Deliberately *not* wired into ``deterministic_validator``: mixing the two would
    make it impossible to say which layer rejected a payload, and would let a flaky
    judge masquerade as a schema failure.
    """

    requirements: list[SemanticRequirement] = field(default_factory=list)
    max_checks: int = DEFAULT_MAX_CHECKS
    #: Tokens spent across all checks (including the ones that blocked).
    tokens_spent: int = 0
    #: How many judgements were actually bought.
    checks_run: int = 0
    #: Names of the gates that produced a finding, for reporting.
    history: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ setup
    def add(
        self,
        name: str,
        requirement: str,
        gate: SemanticGate,
        *,
        severity: Severity = "BLOCK",
    ) -> None:
        self.requirements.append(
            SemanticRequirement(
                name=name, requirement=requirement, gate=gate, severity=severity
            )
        )

    def reset(self) -> None:
        self.tokens_spent = 0
        self.checks_run = 0
        self.history.clear()

    # ------------------------------------------------------------------- run
    def check(self, payload: Any) -> list[Finding]:
        """Evaluate every requirement, returning findings.

        Budget exhaustion is a BLOCK finding, not a silent skip: "we could not
        afford to verify this" must never read as "this is verified".
        """
        findings: list[Finding] = []
        for requirement in self.requirements:
            if self.checks_run >= self.max_checks:
                findings.append(
                    Finding(
                        failed_gate=f"{ERROR_SEMANTIC}.budget",
                        message=(
                            f"semantic budget exhausted: max_checks={self.max_checks}, "
                            f"requirement {requirement.name!r} was never evaluated"
                        ),
                        resolution_hint=(
                            "提高 max_checks，或把这条要求改写成确定性规则 —— "
                            "未评估不等于通过"
                        ),
                        severity=requirement.severity,
                    )
                )
                continue

            try:
                verdict = requirement.gate.evaluate(payload, requirement.requirement)
            except Exception as exc:  # noqa: BLE001 - a broken gate must not pass
                self.checks_run += 1
                findings.append(
                    Finding(
                        failed_gate=f"{ERROR_SEMANTIC}.{requirement.name}.error",
                        message=f"semantic gate {requirement.name!r} raised "
                        f"{type(exc).__name__}: {exc}",
                        resolution_hint="先修这个语义闸门本身，它的结论不可信",
                        severity=requirement.severity,
                    )
                )
                self.history.append(requirement.name)
                continue

            self.checks_run += 1
            self.tokens_spent += verdict.tokens
            self.history.append(requirement.name)
            if not verdict.ok:
                findings.append(
                    Finding(
                        failed_gate=f"{ERROR_SEMANTIC}.{requirement.name}",
                        message=verdict.reason or "requirement not satisfied",
                        resolution_hint=requirement.requirement,
                        severity=requirement.severity,
                    )
                )
        return findings

    # ---------------------------------------------------------------- report
    def to_dict(self) -> dict[str, Any]:
        return {
            "requirements": [r.name for r in self.requirements],
            "max_checks": self.max_checks,
            "checks_run": self.checks_run,
            "tokens_spent": self.tokens_spent,
            "gates_evaluated": list(self.history),
        }


def build_judge(
    llm: LLMAdapter,
    requirements: Sequence[tuple[str, str]],
    *,
    max_checks: int = DEFAULT_MAX_CHECKS,
) -> SemanticLayer:
    """Convenience: one judge per requirement, all sharing the same model."""
    gate = LLMJudgeGate(llm)
    layer = SemanticLayer(max_checks=max_checks)
    for name, requirement in requirements:
        layer.add(name, requirement, gate)
    return layer
