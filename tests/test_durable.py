"""Cross-process resume: a run started in one process finishes in another.

The honest version of this test
-------------------------------
Calling ``resume()`` on a result object in the same process proves nothing about
durability — the object is still in memory. What matters is that process B, which
never saw process A, can pick up the work.

So the test does exactly that: it spawns a subprocess that runs half a task and
persists a snapshot, then resumes from the file in this process. The only thing
shared between them is JSON on disk.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from harness_core.durable import (
    SNAPSHOT_VERSION,
    ResumeError,
    RunSnapshot,
    restore_into,
    snapshot_of,
)
from harness_core.errors import HarnessInfrastructureError
from harness_core.state import AgentHarnessState, HistoryEntry, RunStatus

ROOT = Path(__file__).resolve().parent.parent


def _state(**overrides) -> AgentHarnessState:
    data = {"task_prompt": "settle the order", "tool_whitelist": ["fetch_order"]}
    data.update(overrides)
    return AgentHarnessState(**data)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# snapshot round trip
# ---------------------------------------------------------------------------


def test_a_snapshot_round_trips(tmp_path: Path) -> None:
    state = _state()
    state.history.append(HistoryEntry(attempt=1, phase="p", status="RUNNING"))
    state.retry_count = 2
    path = snapshot_of(state, step=3, action="retry").save(tmp_path / "s.json")

    loaded = RunSnapshot.load(path)
    assert loaded.step == 3
    assert loaded.action == "retry"
    assert loaded.state.task_prompt == "settle the order"
    assert loaded.state.retry_count == 2
    assert len(loaded.state.history) == 1


def test_the_snapshot_file_is_human_readable(tmp_path: Path) -> None:
    """A resume artifact people cannot read is one they cannot debug."""
    path = snapshot_of(_state(), step=1).save(tmp_path / "s.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["snapshot_version"] == SNAPSHOT_VERSION
    assert data["state"]["task_prompt"] == "settle the order"


def test_a_missing_snapshot_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ResumeError, match="not found"):
        RunSnapshot.load(tmp_path / "nope.json")


def test_invalid_json_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ResumeError, match="not valid JSON"):
        RunSnapshot.load(path)


def test_a_snapshot_without_a_version_is_rejected() -> None:
    with pytest.raises(ResumeError, match="snapshot_version"):
        RunSnapshot.from_dict({"state": {}})


def test_a_future_snapshot_is_refused() -> None:
    with pytest.raises(ResumeError, match="upgrade harness-agent-core"):
        RunSnapshot.from_dict({"snapshot_version": SNAPSHOT_VERSION + 1, "state": {}})


def test_a_snapshot_without_state_is_rejected() -> None:
    with pytest.raises(ResumeError, match="state"):
        RunSnapshot.from_dict({"snapshot_version": SNAPSHOT_VERSION})


def test_a_corrupt_state_is_reported_as_a_resume_error() -> None:
    with pytest.raises(ResumeError, match="cannot be rebuilt"):
        RunSnapshot.from_dict(
            {
                "snapshot_version": SNAPSHOT_VERSION,
                "state": {"status": "NOT_A_REAL_STATUS", "task_prompt": "x"},
            }
        )


def test_resume_error_is_an_infrastructure_error() -> None:
    """A broken snapshot must escape the loop, not look like a task failure."""
    assert issubclass(ResumeError, HarnessInfrastructureError)


# ---------------------------------------------------------------------------
# restoring into a live state
# ---------------------------------------------------------------------------


def test_restore_mutates_the_target_object() -> None:
    """Callers hold a reference; returning a new object would leave it stale."""
    source = _state()
    source.retry_count = 5
    snapshot = snapshot_of(source)
    target = _state(task_prompt="different")
    returned = restore_into(snapshot, target)
    assert returned is target
    assert target.task_prompt == "settle the order"
    assert target.retry_count == 5


def test_restore_copies_history_and_status() -> None:
    source = _state(status=RunStatus.PENDING_HUMAN_APPROVAL)
    source.history.append(HistoryEntry(attempt=1, phase="p", status="FAILED"))
    target = _state()
    restore_into(snapshot_of(source), target)
    assert target.status is RunStatus.PENDING_HUMAN_APPROVAL
    assert len(target.history) == 1


def test_restore_keeps_the_trace_id() -> None:
    """The resumed half must report the same trace id as the first half."""
    source = _state()
    snapshot = snapshot_of(source)
    target = _state()
    restore_into(snapshot, target)
    assert target.trace_id == source.trace_id


# ---------------------------------------------------------------------------
# the real thing: two processes
# ---------------------------------------------------------------------------

PRODUCER = textwrap.dedent(
    """
    import json, sys
    sys.path.insert(0, {root!r})

    from harness_core.adapters.llm import FakeLLMAdapter, LLMResponse
    from harness_core.adapters.mcp import FakeMCPConnector
    from harness_core.durable import snapshot_of
    from harness_core.state import AgentHarnessState, HistoryEntry, RunStatus

    state = AgentHarnessState(
        task_prompt="settle order ORD-1001",
        tool_whitelist=["fetch_order"],
        status=RunStatus.PENDING_HUMAN_APPROVAL,
    )
    state.history.append(
        HistoryEntry(attempt=1, phase="guard_tool_call", status="FAILED",
                     error_summary="tool.not_whitelisted: delete_resource")
    )
    state.retry_count = 1
    state.execution_result = {{"order_id": "ORD-1001", "subtotal": 1200.0}}
    state.metadata["approval_request_id"] = "req-1"
    state.metadata["semantic"] = {{"tokens_spent": 12}}

    snapshot_of(state, step=2, action="suspend").save({out!r})
    print("PRODUCED")
    """
)


def test_a_run_started_in_another_process_can_be_resumed(tmp_path: Path) -> None:
    """The whole point: process B never saw process A's objects."""
    out = tmp_path / "snapshot.json"
    result = subprocess.run(
        [sys.executable, "-c", PRODUCER.format(root=str(ROOT), out=str(out))],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "PRODUCED" in result.stdout
    assert out.exists()

    # Process B: rebuild from the file alone.
    snapshot = RunSnapshot.load(out)
    fresh = AgentHarnessState(task_prompt="placeholder", tool_whitelist=[])
    restore_into(snapshot, fresh)

    assert fresh.task_prompt == "settle order ORD-1001"
    assert fresh.status is RunStatus.PENDING_HUMAN_APPROVAL
    assert fresh.retry_count == 1
    assert fresh.execution_result == {"order_id": "ORD-1001", "subtotal": 1200.0}
    assert len(fresh.history) == 1
    assert "delete_resource" in (fresh.history[0].error_summary or "")
    assert fresh.metadata["approval_request_id"] == "req-1"
    assert fresh.metadata["semantic"]["tokens_spent"] == 12


def test_the_resumed_state_can_finish_the_run() -> None:
    """Rebuilding the state is only useful if the engine can continue from it."""
    from harness_core.adapters.llm import FakeLLMAdapter, LLMResponse
    from harness_core.adapters.mcp import FakeMCPConnector
    from harness_core.engine import HarnessOrchestrationEngine
    from harness_core.observability import StructuredLogger
    from harness_core.scenarios import ORDER_OUTPUT_SCHEMA

    source = _state()
    source.history.append(HistoryEntry(attempt=1, phase="p", status="RUNNING"))
    source.retry_count = 1
    snapshot = snapshot_of(source, step=1)

    engine = HarnessOrchestrationEngine(
        llm=FakeLLMAdapter(
            [
                LLMResponse(
                    content={
                        "order_id": "ORD-1001",
                        "currency": "CNY",
                        "amount": 1200.0,
                        "status": "settled",
                    }
                )
            ]
        ),
        connector=FakeMCPConnector(),
        output_schema=ORDER_OUTPUT_SCHEMA,
        logger=StructuredLogger(enabled=False),
    )
    restored = restore_into(snapshot, _state(task_prompt="placeholder"))
    result = engine.run_with_self_healing(restored)
    assert result.ok, result.failure_reason
    # The resumed half continues the same history rather than starting a new one.
    assert len(restored.history) >= 2
    assert restored.trace_id == source.trace_id


# ---------------------------------------------------------------------------
# the LangGraph checkpointer
# ---------------------------------------------------------------------------


pytest.importorskip("langgraph")


def test_a_durable_langgraph_walk_persists_a_snapshot() -> None:
    from langgraph.checkpoint.memory import MemorySaver

    from harness_core.adapters.llm import FakeLLMAdapter, LLMResponse
    from harness_core.adapters.mcp import FakeMCPConnector
    from harness_core.engine import HarnessOrchestrationEngine
    from harness_core.loop_langgraph import LangGraphLoopDriver
    from harness_core.observability import StructuredLogger

    engine = HarnessOrchestrationEngine(
        llm=FakeLLMAdapter([LLMResponse(content={"ok": True})]),
        connector=FakeMCPConnector(),
        logger=StructuredLogger(enabled=False),
    )
    driver = LangGraphLoopDriver(checkpointer=MemorySaver())
    state = engine.build_state("task", tool_whitelist=[])
    result = engine.run_with_self_healing(state, driver=driver)

    assert result.ok
    persisted = driver.load_snapshot(state.trace_id)
    assert persisted is not None
    assert persisted["snapshot_version"] == SNAPSHOT_VERSION
    assert persisted["state"]["trace_id"] == state.trace_id


def test_a_durable_walk_does_not_serialise_the_context() -> None:
    """msgpack cannot hold a LoopContext; the durable mode must not try."""
    from langgraph.checkpoint.memory import MemorySaver

    from harness_core.adapters.llm import FakeLLMAdapter, LLMResponse
    from harness_core.adapters.mcp import FakeMCPConnector
    from harness_core.engine import HarnessOrchestrationEngine
    from harness_core.loop_langgraph import LangGraphLoopDriver
    from harness_core.observability import StructuredLogger

    engine = HarnessOrchestrationEngine(
        llm=FakeLLMAdapter([LLMResponse(content={"ok": True})]),
        connector=FakeMCPConnector(),
        logger=StructuredLogger(enabled=False),
    )
    driver = LangGraphLoopDriver(checkpointer=MemorySaver())
    state = engine.build_state("task", tool_whitelist=[])
    # Would raise "Type is not msgpack serializable: LoopContext" if ctx leaked in.
    engine.run_with_self_healing(state, driver=driver)
    snapshot = driver.load_snapshot(state.trace_id)
    assert "ctx" not in (snapshot or {})


def test_the_driver_reports_whether_it_is_durable() -> None:
    from harness_core.loop_langgraph import LangGraphLoopDriver

    assert LangGraphLoopDriver().durable is False
    assert LangGraphLoopDriver(checkpointer=object()).durable is True


def test_save_and_load_snapshot_require_a_checkpointer() -> None:
    from harness_core.loop_langgraph import LangGraphLoopDriver

    driver = LangGraphLoopDriver()
    with pytest.raises(RuntimeError, match="checkpointer"):
        driver.load_snapshot("x")
