"""End-to-end integration tests — the acceptance criteria, as executable checks.

Each test here maps to one line of the product spec, and each asserts a *terminal*
behaviour rather than an internal call. If these pass, the demo and the CI gate are
telling the truth.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness_core.adapters.llm import FakeLLMAdapter, LLMResponse
from harness_core.engine import HarnessOrchestrationEngine
from harness_core.hitl import AutoApprover, FakeWebhookNotifier, HumanApprovalGateway
from harness_core.observability import JsonlSink, MemorySink, StructuredLogger, Tracer
from harness_core.scenarios import (
    SCENARIOS,
    build_connector,
    build_engine,
    build_output_guard,
    build_tool_specs,
    run_all,
)
from harness_core.state import RunStatus

from .helpers import VALID, response_for, tool_response


# ---------------------------------------------------------------------------
# AC1 — whitelist denial never reaches the connector
# ---------------------------------------------------------------------------


def test_ac1_whitelist_denial_never_executes(engine_factory) -> None:
    engine, _ = engine_factory(
        [tool_response("delete_resource", {"id": "everything"}), response_for()]
    )
    result = engine.run("cleanup", tool_whitelist=["fetch_order"])
    assert result.ok
    assert engine.connector.call_count("delete_resource") == 0  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# AC2 — schema failure produces an actionable, structured message
# ---------------------------------------------------------------------------


def test_ac2_schema_failure_is_structured(engine_factory) -> None:
    engine, _ = engine_factory([response_for({"order_id": "ORD-1"})])
    result = engine.run("settle", tool_whitelist=["fetch_order"])
    assert result.error_stack is not None
    gates = {finding.failed_gate for finding in result.error_stack.blocking}
    assert "output.schema.required" in gates
    # Every finding must carry a repair instruction, not just a complaint.
    assert all(finding.hint for finding in result.error_stack.blocking)


# ---------------------------------------------------------------------------
# AC3 — self-healing succeeds and the repair prompt is what made it possible
# ---------------------------------------------------------------------------


def test_ac3_self_healing_success_depends_on_the_repair_prompt(engine_factory) -> None:
    seen_prompts: list[str] = []

    def responder(prompt: str, state, attempt: int) -> LLMResponse:
        seen_prompts.append(prompt)
        # "Fix it" only after the harness told the model what was wrong.
        if "output.schema.enum" in prompt:
            return response_for()
        return response_for({**VALID, "currency": "EUR"})

    engine, _ = engine_factory(responder=responder, max_retries=3)
    result = engine.run("settle", tool_whitelist=["fetch_order"])

    assert result.status is RunStatus.SELF_HEALED
    assert len(seen_prompts) == 2
    assert "output.schema.enum" in seen_prompts[1]


# ---------------------------------------------------------------------------
# AC4 — retry cap
# ---------------------------------------------------------------------------


def test_ac4_retry_cap_is_enforced(engine_factory) -> None:
    engine, llm = engine_factory(
        [
            response_for({**VALID, "order_id": "A"}),
            response_for({**VALID, "currency": "EUR"}),
            response_for({**VALID, "status": "unknown"}),
            response_for({**VALID, "amount": -1}),
        ],
        max_retries=2,
    )
    result = engine.run("settle", tool_whitelist=["fetch_order"])
    assert result.status is RunStatus.FAILED_WITH_CIRCUIT_BREAKER
    assert llm.prompt_count == 2
    assert result.error_stack is not None


# ---------------------------------------------------------------------------
# AC5 — HITL: suspend, notify, approve, reject
# ---------------------------------------------------------------------------


def test_ac5_hitl_full_cycle(webhook: FakeWebhookNotifier) -> None:
    connector = build_connector()

    # (a) approve
    approve_gateway = HumanApprovalGateway(webhook, handler=AutoApprover("approve"))
    approve_llm = FakeLLMAdapter(
        [
            tool_response("write_api", {"order_id": "ORD-1001", "amount": 1200.0}),
            response_for(),
        ]
    )
    approved = build_engine(approve_llm, connector=connector, gateway=approve_gateway).run(
        "settle", tool_whitelist=["fetch_order", "write_api"]
    )
    assert approved.ok
    assert webhook.count == 1

    # (b) reject
    reject_gateway = HumanApprovalGateway(webhook, handler=AutoApprover("reject", "nope"))
    reject_llm = FakeLLMAdapter(
        [tool_response("write_api", {"order_id": "ORD-1001", "amount": 1200.0})]
    )
    rejected = build_engine(reject_llm, connector=connector, gateway=reject_gateway).run(
        "settle", tool_whitelist=["fetch_order", "write_api"]
    )
    assert rejected.status is RunStatus.REJECTED_BY_HUMAN
    assert connector.call_count("write_api") == 1  # only the approved one ran
    assert webhook.count == 2


# ---------------------------------------------------------------------------
# AC6 — trace completeness
# ---------------------------------------------------------------------------


def test_ac6_trace_has_every_documented_field(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    sink = JsonlSink(path)
    engine = build_engine(
        FakeLLMAdapter([response_for({**VALID, "currency": "EUR"}), response_for()]),
        max_retries=2,
    )
    engine.tracer = Tracer(sink=sink)
    result = engine.run("settle", tool_whitelist=["fetch_order"])
    assert result.ok

    record = json.loads(path.read_text(encoding="utf-8").strip())
    assert record["trace_id"] == result.trace_id
    assert record["status"] == "SELF_HEALED"
    assert record["attempts"] == 2
    assert record["retry_count"] == 1
    assert record["tokens_estimate"] > 0
    assert record["total_duration_ms"] >= 0
    assert record["spans"], "a run must record at least one span"

    kinds = {span["name"] for span in record["spans"]}
    assert {"attempt", "llm.generate", "validate", "retry"} <= kinds

    for span in record["spans"]:
        assert span["duration_ms"] >= 0
        assert isinstance(span["tokens_estimate"], int)
        assert span["status"]


# ---------------------------------------------------------------------------
# AC7 — the eval suite is a real gate
# ---------------------------------------------------------------------------


def test_ac7_every_scenario_reports_a_terminal_status() -> None:
    for outcome in run_all():
        assert outcome.passed, f"{outcome.name} failed: {outcome.failure_reason}"
        assert RunStatus(outcome.status).is_terminal


def test_ac7_scenarios_are_independent() -> None:
    """Running one scenario must not leak state into another."""
    first = SCENARIOS["hitl_approve"].run()
    second = SCENARIOS["hitl_reject"].run()
    third = SCENARIOS["hitl_approve"].run()
    assert first.passed and second.passed and third.passed
    assert first.trace_id != second.trace_id != third.trace_id


# ---------------------------------------------------------------------------
# Cross-cutting: the demo and the tests must agree
# ---------------------------------------------------------------------------


def test_scenario_statuses_match_the_documented_table() -> None:
    """The README documents these terminal statuses; keep them honest."""
    documented = {
        "self_healing_chain": "SELF_HEALED",
        "circuit_breaker_exhausted": "FAILED_WITH_CIRCUIT_BREAKER",
        "circuit_breaker_recurred": "FAILED_WITH_CIRCUIT_BREAKER",
        "hitl_approve": "FIRST_PASS",
        "hitl_reject": "REJECTED_BY_HUMAN",
        "hitl_pending_then_resume": "FIRST_PASS",
        "warnings_do_not_retry": "OK_WITH_WARNINGS",
        "record_replay_equivalence": "SELF_HEALED",
        # Both injection scenarios end in OK_WITH_WARNINGS, not a clean pass: the
        # detected injection is preserved as a warning and surfaces in the terminal
        # status. That is deliberate — a run that saw an injection attempt must not
        # look identical to a run that did not.
        "injection_overreach": "OK_WITH_WARNINGS",
        "injection_is_fenced": "OK_WITH_WARNINGS",
    }
    assert set(documented) == set(SCENARIOS)
    actual = {outcome.name: outcome.status for outcome in run_all()}
    assert actual == documented


def test_engine_can_be_reused_for_several_runs(sink: MemorySink) -> None:
    engine = build_engine(
        FakeLLMAdapter([response_for(), response_for({**VALID, "currency": "EUR"}), response_for()]),
        max_retries=2,
    )
    engine.tracer = Tracer(sink=sink)
    first = engine.run("task one", tool_whitelist=["fetch_order"])
    second = engine.run("task two", tool_whitelist=["fetch_order"])

    assert first.ok and second.ok
    assert first.trace_id != second.trace_id
    assert len(sink.records) == 2
    # The second run starts from a clean state (retry_count is not carried over).
    assert second.state is not None and second.state.retry_count == 1


def test_engine_does_not_share_mutable_state_between_runs() -> None:
    llm = FakeLLMAdapter([response_for()])
    engine = build_engine(llm)
    first = engine.run("a", tool_whitelist=["fetch_order"])
    second = engine.run("b", tool_whitelist=["fetch_order"])
    assert first.state is not second.state
    assert first.state.history is not second.state.history


def test_output_guard_is_reusable_across_engines() -> None:
    guard = build_output_guard()
    connector = build_connector()
    first = HarnessOrchestrationEngine(
        llm=FakeLLMAdapter([response_for()]),
        connector=connector,
        output_guard=guard,
        tool_specs=build_tool_specs(),
        logger=StructuredLogger(enabled=False),
    )
    second = HarnessOrchestrationEngine(
        llm=FakeLLMAdapter([response_for({**VALID, "currency": "EUR"}), response_for()]),
        connector=connector,
        output_guard=guard,
        tool_specs=build_tool_specs(),
        logger=StructuredLogger(enabled=False),
    )
    assert first.run("a", tool_whitelist=["fetch_order"]).ok
    assert second.run("b", tool_whitelist=["fetch_order"]).status is RunStatus.SELF_HEALED


@pytest.mark.parametrize("severity", ["WARN", "INFO"])
def test_non_blocking_severities_never_fail_the_run(engine_factory, severity: str) -> None:
    engine, llm = engine_factory([response_for()])
    engine.output_guard.register_rule("advisory", lambda payload: False, severity=severity)
    result = engine.run("settle", tool_whitelist=["fetch_order"])
    assert result.ok
    assert llm.prompt_count == 1
    assert result.warnings
