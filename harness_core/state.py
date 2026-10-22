"""The harness state machine's data model.

The state is a plain mutable dataclass rather than a graph-owned dict. That is a
deliberate decision (see README "Design decisions"): if the state lives inside a
graph framework, any node that returns a partial dict can silently drop fields the
harness relies on. The harness owns the state, mutates it explicitly, and hands a
snapshot to the planner on every turn.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from .errors import ErrorStack


class RunStatus(str, Enum):
    """Terminal and intermediate states of one harness run."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    #: Suspended: a high-risk tool call is waiting for a human decision.
    PENDING_HUMAN_APPROVAL = "PENDING_HUMAN_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    #: Succeeded on the first attempt (no self-healing needed).
    FIRST_PASS = "FIRST_PASS"
    #: Succeeded after >=1 repair round.
    SELF_HEALED = "SELF_HEALED"
    #: Succeeded, but with WARN-level findings that did not block delivery.
    OK_WITH_WARNINGS = "OK_WITH_WARNINGS"
    FAILED = "FAILED"
    #: max_retries exhausted, or the same failure repeated.
    FAILED_WITH_CIRCUIT_BREAKER = "FAILED_WITH_CIRCUIT_BREAKER"
    #: A human rejected the suspended high-risk action.
    REJECTED_BY_HUMAN = "REJECTED_BY_HUMAN"
    #: Approval was requested but never answered (timeout / no notifier).
    APPROVAL_TIMEOUT = "APPROVAL_TIMEOUT"

    @property
    def is_success(self) -> bool:
        return self in {
            RunStatus.SUCCEEDED,
            RunStatus.FIRST_PASS,
            RunStatus.SELF_HEALED,
            RunStatus.OK_WITH_WARNINGS,
        }

    @property
    def is_terminal(self) -> bool:
        return self not in {RunStatus.PENDING, RunStatus.RUNNING}


Decision = Literal["allow", "deny", "require_approval"]


@dataclass
class ToolCall:
    """A model's *request* to run a tool. Never an authorisation by itself."""

    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    #: Set by the guard, not by the model.
    decision: Decision | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "decision": self.decision,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolCall":
        """Rebuild a pending call from a snapshot.

        ``decision`` is carried across on purpose: a call that was already authorised
        must not be re-judged on resume, or an approval could be lost (or granted
        twice) depending on where the process restarted.
        """
        raw_decision = data.get("decision")
        decision: Decision | None = (
            raw_decision if raw_decision in ("allow", "require_approval", "deny") else None
        )
        return cls(
            tool_name=str(data.get("tool_name", "")),
            arguments=dict(data.get("arguments") or {}),
            call_id=str(data.get("call_id") or uuid.uuid4().hex[:12]),
            decision=decision,
            reason=data.get("reason"),
        )


@dataclass
class HistoryEntry:
    """One attempt in the self-healing loop.

    ``history`` is what the harness feeds back into the next prompt; it is never
    reconstructed from a graph framework's return value.
    """

    attempt: int
    phase: str
    status: str
    tool_name: str | None = None
    error_summary: str | None = None
    duration_ms: float = 0.0
    tokens_estimate: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "phase": self.phase,
            "status": self.status,
            "tool_name": self.tool_name,
            "error_summary": self.error_summary,
            "duration_ms": round(self.duration_ms, 3),
            "tokens_estimate": self.tokens_estimate,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HistoryEntry":
        """Rebuild one entry from a snapshot.

        ``status`` stays a plain ``str`` rather than being coerced to an enum: the
        field has always been a string, and turning it into an enum here would make
        old snapshots — and every caller that compares against ``"FAILED"`` —
        silently change behaviour.
        """
        return cls(
            attempt=int(data.get("attempt", 0)),
            phase=str(data.get("phase", "")),
            status=str(data.get("status", "")),
            tool_name=data.get("tool_name"),
            error_summary=data.get("error_summary"),
            duration_ms=float(data.get("duration_ms", 0.0)),
            tokens_estimate=int(data.get("tokens_estimate", 0)),
        )


@dataclass
class AgentHarnessState:
    """Everything the engine needs to drive one task to a deterministic outcome.

    The first ten fields are the contract required by the product spec; the rest
    are supporting data that the guardrails, the breaker and the tracer need.
    """

    # --- required contract -------------------------------------------------
    task_prompt: str
    tool_whitelist: list[str]
    current_plan: dict[str, Any] | None = None
    execution_result: Any = None
    error_stack: ErrorStack | None = None
    retry_count: int = 0
    max_retries: int = 3
    history: list[HistoryEntry] = field(default_factory=list)
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: RunStatus = RunStatus.PENDING

    # --- supporting data ---------------------------------------------------
    #: Tool argument JSON Schemas, keyed by tool name (from config).
    tool_schemas: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Tools that must be approved by a human before execution.
    high_risk_tools: set[str] = field(default_factory=set)
    #: The tool call the model asked for on the current attempt.
    pending_tool_call: ToolCall | None = None
    #: Findings that did not block (WARN / INFO) and are reported at the end.
    warnings: list[Any] = field(default_factory=list)
    #: Human decision for the suspended call: "approve" / "reject" / None.
    approval_decision: str | None = None
    approval_reason: str | None = None
    #: Why the run stopped, when it did not succeed.
    failure_reason: str | None = None
    #: Free-form scratch space for adapters / custom validators.
    metadata: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ API
    def record(self, entry: HistoryEntry) -> None:
        self.history.append(entry)

    def last_history(self, n: int = 1) -> list[HistoryEntry]:
        return self.history[-n:] if n > 0 else []

    def fail(self, stack: ErrorStack, status: RunStatus = RunStatus.FAILED) -> None:
        self.error_stack = stack
        self.status = status

    def to_dict(self) -> dict[str, Any]:
        """Serialisable snapshot (safe for JSONL traces and resumes)."""
        return {
            "trace_id": self.trace_id,
            "task_prompt": self.task_prompt,
            "tool_whitelist": list(self.tool_whitelist),
            "current_plan": self.current_plan,
            "execution_result": self.execution_result,
            "error_stack": self.error_stack.to_dict() if self.error_stack else None,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "history": [entry.to_dict() for entry in self.history],
            "status": self.status.value,
            "high_risk_tools": sorted(self.high_risk_tools),
            "pending_tool_call": (
                self.pending_tool_call.to_dict() if self.pending_tool_call else None
            ),
            "approval_decision": self.approval_decision,
            "failure_reason": self.failure_reason,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentHarnessState":
        """Rebuild a state from a :meth:`to_dict` snapshot.

        This is what makes ``resume()`` work across processes: without it a snapshot
        is write-only, and "durable resume" would mean "keep the object in memory",
        which is exactly the limitation it is supposed to remove.

        Unknown keys are ignored rather than fatal: a snapshot written by a newer
        build should still resume on an older one for the fields it understands,
        and failing here would make forward compatibility impossible.
        """
        raw_stack = data.get("error_stack")
        error_stack = (
            ErrorStack.from_dict(raw_stack) if isinstance(raw_stack, dict) else None
        )
        raw_pending = data.get("pending_tool_call")
        pending = (
            ToolCall.from_dict(raw_pending) if isinstance(raw_pending, dict) else None
        )
        raw_status = data.get("status", RunStatus.PENDING.value)
        try:
            status = RunStatus(str(raw_status))
        except ValueError:
            # An unknown status must not silently become PENDING: that would make a
            # finished run look resumable.
            raise ValueError(f"unknown run status in snapshot: {raw_status!r}") from None

        return cls(
            task_prompt=str(data.get("task_prompt", "")),
            tool_whitelist=list(data.get("tool_whitelist") or []),
            current_plan=data.get("current_plan"),
            execution_result=data.get("execution_result"),
            error_stack=error_stack,
            retry_count=int(data.get("retry_count", 0)),
            max_retries=int(data.get("max_retries", 3)),
            history=[HistoryEntry.from_dict(entry) for entry in data.get("history") or []],
            trace_id=str(data.get("trace_id") or uuid.uuid4().hex),
            status=status,
            high_risk_tools=set(data.get("high_risk_tools") or []),
            pending_tool_call=pending,
            warnings=list(data.get("warnings") or []),
            approval_decision=data.get("approval_decision"),
            approval_reason=data.get("approval_reason"),
            failure_reason=data.get("failure_reason"),
            metadata=dict(data.get("metadata") or {}),
        )
