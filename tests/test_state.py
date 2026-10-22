"""State model tests: required fields, serialisation, status semantics."""

from __future__ import annotations

import json

from harness_core.errors import ErrorStack, Finding
from harness_core.state import AgentHarnessState, HistoryEntry, RunStatus, ToolCall


def test_state_has_every_required_field() -> None:
    state = AgentHarnessState(task_prompt="t", tool_whitelist=["a"])
    for field_name in (
        "task_prompt",
        "tool_whitelist",
        "current_plan",
        "execution_result",
        "error_stack",
        "retry_count",
        "max_retries",
        "history",
        "trace_id",
        "status",
    ):
        assert hasattr(state, field_name), f"missing required field {field_name}"


def test_state_defaults() -> None:
    state = AgentHarnessState(task_prompt="t", tool_whitelist=[])
    assert state.retry_count == 0
    assert state.max_retries == 3
    assert state.history == []
    assert state.status is RunStatus.PENDING
    assert state.trace_id


def test_trace_ids_are_unique_per_state() -> None:
    first = AgentHarnessState(task_prompt="a", tool_whitelist=[])
    second = AgentHarnessState(task_prompt="b", tool_whitelist=[])
    assert first.trace_id != second.trace_id


def test_record_and_last_history() -> None:
    state = AgentHarnessState(task_prompt="t", tool_whitelist=[])
    state.record(HistoryEntry(attempt=1, phase="p", status="FAILED"))
    state.record(HistoryEntry(attempt=2, phase="p", status="FIRST_PASS"))
    assert len(state.history) == 2
    assert state.last_history(1)[0].attempt == 2
    assert state.last_history(0) == []


def test_fail_sets_status_and_stack() -> None:
    state = AgentHarnessState(task_prompt="t", tool_whitelist=[])
    stack = ErrorStack(step="s", findings=[Finding(failed_gate="g", message="m")])
    state.fail(stack, RunStatus.FAILED_WITH_CIRCUIT_BREAKER)
    assert state.status is RunStatus.FAILED_WITH_CIRCUIT_BREAKER
    assert state.error_stack is stack


def test_to_dict_is_json_serialisable() -> None:
    state = AgentHarnessState(task_prompt="t", tool_whitelist=["a"], high_risk_tools={"write_api"})
    state.pending_tool_call = ToolCall("write_api", {"amount": 1})
    state.record(HistoryEntry(attempt=1, phase="p", status="RUNNING"))
    payload = state.to_dict()
    text = json.dumps(payload, ensure_ascii=False)
    restored = json.loads(text)

    assert restored["trace_id"] == state.trace_id
    assert restored["tool_whitelist"] == ["a"]
    assert restored["high_risk_tools"] == ["write_api"]
    assert restored["status"] == "PENDING"
    assert restored["pending_tool_call"]["tool_name"] == "write_api"
    assert len(restored["history"]) == 1


def test_status_success_set() -> None:
    for status in (
        RunStatus.SUCCEEDED,
        RunStatus.FIRST_PASS,
        RunStatus.SELF_HEALED,
        RunStatus.OK_WITH_WARNINGS,
    ):
        assert status.is_success, f"{status} should be a success"
    for status in (
        RunStatus.FAILED,
        RunStatus.FAILED_WITH_CIRCUIT_BREAKER,
        RunStatus.PENDING_HUMAN_APPROVAL,
        RunStatus.REJECTED_BY_HUMAN,
    ):
        assert not status.is_success


def test_status_terminal_set() -> None:
    assert not RunStatus.PENDING.is_terminal
    assert not RunStatus.RUNNING.is_terminal
    assert RunStatus.SELF_HEALED.is_terminal
    assert RunStatus.FAILED_WITH_CIRCUIT_BREAKER.is_terminal


def test_tool_call_defaults_to_no_decision() -> None:
    call = ToolCall("fetch_order", {"order_id": "ORD-1"})
    assert call.decision is None, "the model's request must not imply authorisation"
    assert call.call_id


def test_history_entry_roundtrip() -> None:
    entry = HistoryEntry(
        attempt=2,
        phase="p",
        status="FAILED",
        tool_name="fetch_order",
        error_summary="boom",
        duration_ms=1.23456,
        tokens_estimate=12,
    )
    payload = entry.to_dict()
    assert payload["duration_ms"] == 1.235
    assert payload["tool_name"] == "fetch_order"


def test_error_stack_roundtrip() -> None:
    stack = ErrorStack(
        step="validate",
        findings=[
            Finding(failed_gate="g", message="m", field="$.a", severity="WARN"),
            Finding(failed_gate="h", message="n"),
        ],
    )
    restored = ErrorStack.from_dict(stack.to_dict())
    assert restored.step == "validate"
    assert len(restored.findings) == 2
    assert restored.findings[0].severity == "WARN"
    assert len(restored.blocking) == 1
    assert len(restored.warnings) == 1


def test_error_stack_signature_is_order_independent() -> None:
    first = ErrorStack(
        step="s",
        findings=[
            Finding(failed_gate="a", message="1"),
            Finding(failed_gate="b", message="2"),
        ],
    )
    second = ErrorStack(
        step="s",
        findings=[
            Finding(failed_gate="b", message="2"),
            Finding(failed_gate="a", message="1"),
        ],
    )
    assert first.signature == second.signature


def test_error_stack_prompt_block_includes_observation_and_hint() -> None:
    stack = ErrorStack(
        step="validate",
        findings=[
            Finding(
                failed_gate="output.schema.enum",
                message="$.currency: 'EUR' not in ['CNY', 'USD']",
                field="$.currency",
                resolution_hint="$.currency 只能取 ['CNY', 'USD'] 之一",
            )
        ],
    )
    block = stack.to_prompt_block()
    assert "output.schema.enum" in block
    assert "EUR" in block
    assert "只能取" in block


def test_error_stack_prompt_block_truncates() -> None:
    stack = ErrorStack(
        step="s",
        findings=[Finding(failed_gate=f"g{i}", message="m") for i in range(12)],
    )
    block = stack.to_prompt_block(max_items=3)
    assert "and 9 more" in block
