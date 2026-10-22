"""Structured failure signals shared by every harness layer.

Why this module exists
----------------------
The naive harness feeds ``traceback.format_exc()`` back to the model. Free text is
not assertable, not deduplicable and not comparable, so the model has to *guess*
where the failure is. A ``Finding`` is the opposite: a machine-readable, stable
fingerprint of one failure that the model can act on and the circuit breaker can
count.

Design notes
------------
* ``severity`` is three-valued on purpose (BLOCK / WARN / INFO). Only BLOCK
  findings consume a retry; WARN is recorded and the run still succeeds. This
  prevents paying tokens to fix things that do not block delivery.
* ``fingerprint`` deliberately excludes ``message`` / ``resolution_hint`` /
  ``trace``: those change every attempt, and including them would make "the same
  failure twice" invisible.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal

Severity = Literal["BLOCK", "WARN", "INFO"]

VALID_SEVERITIES: tuple[str, ...] = ("BLOCK", "WARN", "INFO")


def coerce_severity(value: Any, default: Severity = "BLOCK") -> Severity:
    """Normalise a free-form severity (config file, tag, adapter) to the Literal.

    Unknown values fall back to ``default`` instead of silently becoming an
    arbitrary string: an unrecognised severity must never behave like "not BLOCK"
    and let a failure through.
    """
    text = str(value).strip().upper() if value is not None else ""
    if text in VALID_SEVERITIES:
        return text  # type: ignore[return-value]
    return default

#: Error classes understood by the harness. Kept as plain strings so traces stay
#: readable in JSONL without an enum lookup table.
ERROR_TOOL_NOT_ALLOWED = "tool.not_whitelisted"
ERROR_TOOL_SCHEMA = "tool.invalid_arguments"
ERROR_TOOL_EXECUTION = "tool.execution_error"
ERROR_OUTPUT_SCHEMA = "output.schema"
ERROR_OUTPUT_FORBIDDEN = "output.forbidden_pattern"
ERROR_OUTPUT_RULE = "output.business_rule"
ERROR_LLM = "llm.error"
#: The semantic layer (L5). Kept distinct from every deterministic gate on purpose:
#: a judgement is evidence, a schema violation is a fact, and a reader must be able
#: to tell which one rejected the payload.
ERROR_SEMANTIC = "semantic.judge"
ERROR_UNKNOWN = "internal.unexpected"


class HarnessInfrastructureError(RuntimeError):
    """The harness itself is misconfigured — not the model, not the task.

    This distinction matters in CI. A model returning bad JSON is a *task* failure:
    retry it, count it, report it. A cassette that ran out of entries is an
    *infrastructure* failure: retrying is pointless and reporting it as a business
    failure hides a broken test fixture behind a plausible-looking verdict.

    Adapters raise this to escape the self-healing loop. The engine lets it
    propagate instead of converting it into a ``Finding``.
    """


@dataclass(frozen=True)
class Finding:
    """One actionable failure signal.

    Attributes
    ----------
    failed_gate:
        Which gate rejected the payload, e.g. ``tool.not_whitelisted``,
        ``output.schema.required``, ``policy.amount_limit``.
    message:
        Human-readable detail. Free text, never part of the fingerprint.
    severity:
        ``BLOCK`` must be fixed; ``WARN`` is recorded only; ``INFO`` is advisory.
    tool_name / field / line:
        Optional location hints (which tool, which field, which line).
    resolution_hint:
        One-line instruction that is directly usable inside a repair prompt.
    """

    failed_gate: str
    message: str
    severity: Severity = "BLOCK"
    tool_name: str | None = None
    field: str | None = None
    line: int | None = None
    resolution_hint: str = ""
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.fingerprint:
            # Fingerprint the *shape* of the failure only.
            key = json.dumps(
                [self.failed_gate, self.tool_name, self.field, self.line, self.severity],
                ensure_ascii=False,
                sort_keys=True,
            )
            object.__setattr__(
                self, "fingerprint", hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
            )

    @property
    def hint(self) -> str:
        """The best available repair instruction."""
        return self.resolution_hint or self.message

    def to_dict(self) -> dict[str, Any]:
        return {
            "failed_gate": self.failed_gate,
            "message": self.message,
            "severity": self.severity,
            "tool_name": self.tool_name,
            "field": self.field,
            "line": self.line,
            "resolution_hint": self.resolution_hint,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Finding":
        return cls(
            failed_gate=str(data.get("failed_gate", "unknown")),
            message=str(data.get("message", "")),
            severity=coerce_severity(data.get("severity")),
            tool_name=data.get("tool_name"),
            field=data.get("field"),
            line=data.get("line"),
            resolution_hint=str(data.get("resolution_hint", "")),
            fingerprint=str(data.get("fingerprint", "")),
        )


@dataclass
class ErrorStack:
    """An ordered bundle of findings produced by one failed step.

    ``trace`` is kept as *supporting evidence* only — it is appended to the
    repair prompt after the structured block, never used for deduplication.
    """

    step: str
    findings: list[Finding] = field(default_factory=list)
    trace: str | None = None

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "BLOCK"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "WARN"]

    @property
    def signature(self) -> str:
        """Stable identity of *this failure set*, used by the circuit breaker."""
        return "|".join(sorted({f.fingerprint for f in self.blocking}))

    def extend(self, findings: list[Finding]) -> "ErrorStack":
        self.findings.extend(findings)
        return self

    def to_prompt_block(self, max_items: int = 8) -> str:
        """Compact, model-consumable rendering of the blocking findings.

        Each line carries both the *observation* (``message``: what actually
        failed) and the *instruction* (``resolution_hint``: what to do about it).
        Dropping the observation would hide the real error text — e.g. the
        upstream ``503`` that caused a tool failure.
        """
        lines = [f"failed_step: {self.step}"]
        blocking = self.blocking
        for finding in blocking[:max_items]:
            location = ""
            if self.step or finding.tool_name:
                location = f" ({finding.tool_name or self.step}"
                if finding.field:
                    # JSON paths already start with '$'; don't glue a dot onto them.
                    separator = "" if finding.field.startswith("$") else "."
                    location += f"{separator}{finding.field}"
                if finding.line is not None:
                    location += f":{finding.line}"
                location += ")"
            detail = finding.message
            if finding.resolution_hint and finding.resolution_hint != finding.message:
                detail = f"{finding.message} -> {finding.resolution_hint}"
            lines.append(f"- [{finding.failed_gate}]{location} {detail}")
        if len(blocking) > max_items:
            lines.append(f"- ... and {len(blocking) - max_items} more blocking finding(s)")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "findings": [f.to_dict() for f in self.findings],
            "trace": self.trace,
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ErrorStack":
        return cls(
            step=str(data.get("step", "")),
            findings=[Finding.from_dict(item) for item in data.get("findings", [])],
            trace=data.get("trace"),
        )
