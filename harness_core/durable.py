"""Durable, cross-process resume for the LangGraph driver.

The constraint that shapes this file
------------------------------------
A LangGraph checkpointer serialises the graph state with msgpack. A
:class:`~harness_core.loop.LoopContext` holds the engine, the connector and the
adapters — live objects with sockets and threads. Serialising them is impossible,
and it is also wrong: the whole point of the harness is that the *state* survives
while the *machinery* is rebuilt.

So the graph state carries a **serialisable snapshot** and nothing else::

    {"ctx": <LoopContext>, "step": int, "action": str}   # in-memory walk
    {"snapshot": <dict>,   "step": int, "action": str}   # durable walk

Two shapes, chosen by whether a checkpointer was supplied. A durable walk cannot
carry ``ctx`` at all — msgpack rejects it — so the node rebuilds a context from the
snapshot on entry. That is not a workaround; it is the honest representation of
"the process may not be the one that started this run".

What is deliberately *not* persisted
------------------------------------
The engine, the connector, the tracer and the breaker. Resuming means the caller
supplies a working engine for the resumed half. Pretending to restore a socket would
produce a run that looks resumed but is wired to nothing.

Failure policy
--------------
A snapshot that cannot be rebuilt (unknown status, missing task) raises
``ResumeError`` — never a silent fresh start. Silently starting over would re-run
side-effecting tools, which is the worst possible outcome of a resume bug.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import HarnessInfrastructureError
from .state import AgentHarnessState

#: Schema version for the persisted snapshot. Bumped when the shape changes.
SNAPSHOT_VERSION = 1


class ResumeError(HarnessInfrastructureError):
    """A persisted run could not be rebuilt.

    Subclasses ``HarnessInfrastructureError`` so it escapes the self-healing loop:
    asking the model to "repair" a corrupt snapshot is meaningless, and treating it
    as a task failure would hide a broken store behind a business verdict.
    """


@dataclass
class RunSnapshot:
    """The serialisable half of a run: what happened, not what ran it."""

    state: AgentHarnessState
    step: int = 0
    action: str = "start"
    version: int = SNAPSHOT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_version": self.version,
            "step": self.step,
            "action": self.action,
            "state": self.state.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunSnapshot":
        if not isinstance(data, dict):
            raise ResumeError(f"snapshot must be a mapping, got {type(data).__name__}")
        version = data.get("snapshot_version")
        if version is None:
            raise ResumeError("snapshot is missing 'snapshot_version'")
        if int(version) > SNAPSHOT_VERSION:
            raise ResumeError(
                f"snapshot is version {version}, this build understands "
                f"{SNAPSHOT_VERSION}; upgrade harness-agent-core"
            )
        raw_state = data.get("state")
        if not isinstance(raw_state, dict):
            raise ResumeError("snapshot is missing the 'state' mapping")
        try:
            state = AgentHarnessState.from_dict(raw_state)
        except (ValueError, TypeError, KeyError) as exc:
            raise ResumeError(f"snapshot state cannot be rebuilt: {exc}") from exc
        return cls(
            state=state,
            step=int(data.get("step", 0)),
            action=str(data.get("action", "start")),
            version=int(version),
        )

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return target

    @classmethod
    def load(cls, path: str | Path) -> "RunSnapshot":
        target = Path(path)
        if not target.exists():
            raise ResumeError(f"snapshot not found: {target}")
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ResumeError(f"{target}: snapshot is not valid JSON: {exc}") from exc
        return cls.from_dict(data)


def snapshot_of(state: AgentHarnessState, *, step: int = 0, action: str = "start") -> RunSnapshot:
    """Capture a run for later resumption."""
    return RunSnapshot(state=state, step=step, action=action)


def restore_into(snapshot: RunSnapshot, target: AgentHarnessState) -> AgentHarnessState:
    """Copy a snapshot's fields onto an existing state object.

    Mutates ``target`` rather than returning a new object because callers already
    hold a reference (the engine, the tracer, the driver all share one state). A
    fresh object would leave those references pointing at the old, empty state —
    a bug that looks like "the resume worked but nothing happened".
    """
    rebuilt = snapshot.state
    target.task_prompt = rebuilt.task_prompt
    target.tool_whitelist = list(rebuilt.tool_whitelist)
    target.current_plan = rebuilt.current_plan
    target.execution_result = rebuilt.execution_result
    target.error_stack = rebuilt.error_stack
    target.retry_count = rebuilt.retry_count
    target.max_retries = rebuilt.max_retries
    target.history = list(rebuilt.history)
    target.trace_id = rebuilt.trace_id
    target.status = rebuilt.status
    target.tool_schemas = dict(rebuilt.tool_schemas)
    target.high_risk_tools = set(rebuilt.high_risk_tools)
    target.pending_tool_call = rebuilt.pending_tool_call
    target.warnings = list(rebuilt.warnings)
    target.approval_decision = rebuilt.approval_decision
    target.approval_reason = rebuilt.approval_reason
    target.failure_reason = rebuilt.failure_reason
    target.metadata = dict(rebuilt.metadata)
    return target
