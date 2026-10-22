"""LangGraph ``interrupt()``: hand the HITL suspension to the graph.

What is different from ``engine.resume()``
------------------------------------------
The default path suspends by *returning* from the loop, then resumes by re-entering
it from the top. LangGraph's ``interrupt()`` stops **inside the node**: the
checkpoint records the exact position, and ``Command(resume=...)`` re-executes only
that node with the human's answer.

The tests below pin the behaviours that difference creates:

* the graph really stops mid-node (``is_suspended`` is true, the run is not over);
* the tool runs **exactly once**, on approval — not before, not twice;
* a rejection ends the run as ``REJECTED_BY_HUMAN`` and never touches the tool;
* a non-committal answer ("maybe") is a rejection, not an approval;
* nothing before the suspension re-runs.
"""

from __future__ import annotations

import pytest

pytest.importorskip("langgraph")

from langgraph.checkpoint.memory import MemorySaver

from harness_core.adapters.llm import FakeLLMAdapter, LLMResponse, ToolCallRequest
from harness_core.adapters.mcp import FakeMCPConnector
from harness_core.circuit_breaker import CircuitBreaker
from harness_core.engine import HarnessOrchestrationEngine
from harness_core.loop import LoopContext
from harness_core.loop_langgraph import (
    LangGraphLoopDriver,
    _answer_reason,
    _is_approval,
)
from harness_core.observability import StructuredLogger, Tracer
from harness_core.state import RunStatus

VALID = {"order_id": "ORD-1001", "currency": "CNY", "amount": 1200.0, "status": "settled"}
SCHEMA = {
    "type": "object",
    "required": ["order_id", "currency", "amount", "status"],
    "properties": {
        "order_id": {"type": "string"},
        "currency": {"type": "string", "enum": ["CNY", "USD"]},
        "amount": {"type": "number"},
        "status": {"type": "string", "enum": ["settled", "rejected"]},
    },
}


def _tool_response(name: str, args: dict) -> LLMResponse:
    return LLMResponse(
        tool_call=ToolCallRequest(name, args),
        raw_text=f'{{"tool_name": "{name}"}}',
    )


def _engine(connector: FakeMCPConnector, responses: list[LLMResponse], *, gateway=None):
    from harness_core.guards import OutputGuard

    return HarnessOrchestrationEngine(
        llm=FakeLLMAdapter(responses),
        connector=connector,
        output_guard=OutputGuard(output_schema=SCHEMA),
        approval_gateway=gateway,
        tool_specs=connector.tool_definitions_as_specs()
        if hasattr(connector, "tool_definitions_as_specs")
        else {},
        high_risk_tools={"write_api", "delete_resource"},
        logger=StructuredLogger(enabled=False),
    )


def _ctx(engine: HarnessOrchestrationEngine, whitelist: list[str], *, max_steps: int = 8) -> LoopContext:
    state = engine.build_state("settle the order", tool_whitelist=whitelist)
    return LoopContext(
        engine=engine,
        state=state,
        breaker=CircuitBreaker(),
        tracer=Tracer(),
        max_steps=max_steps,
    )


# ---------------------------------------------------------------------------
# answer interpretation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("answer", ["approve", "APPROVED", "yes", "ok", "true", True])
def test_approval_words_authorise(answer) -> None:
    assert _is_approval(answer) is True


@pytest.mark.parametrize(
    "answer",
    ["reject", "no", "maybe", "later", "", "hmm", False, None, 42, [], {"decision": "no"}],
)
def test_everything_else_is_a_rejection(answer) -> None:
    """A human who says "maybe" must not authorise a destructive tool."""
    assert _is_approval(answer) is False


def test_a_structured_approval_is_accepted() -> None:
    assert _is_approval({"decision": "approve"}) is True
    assert _is_approval({"approved": True}) is True
    assert _is_approval({"answer": "yes"}) is True


def test_a_structured_answer_without_a_known_key_is_a_rejection() -> None:
    assert _is_approval({"whatever": "approve"}) is False


def test_the_humans_own_words_are_preserved() -> None:
    assert _answer_reason({"decision": "no", "reason": "amount looks wrong"}) == (
        "amount looks wrong"
    )
    assert _answer_reason("no way") == "no way"


# ---------------------------------------------------------------------------
# the suspension itself
# ---------------------------------------------------------------------------


def test_interrupt_mode_requires_a_checkpointer() -> None:
    """Without a checkpointer there is nowhere to record the suspension."""
    with pytest.raises(ValueError, match="checkpointer"):
        LangGraphLoopDriver(interrupt_mode=True)


def test_the_graph_stops_inside_the_node(connector) -> None:
    responses = [_tool_response("write_api", {"order_id": "ORD-1001", "amount": 1200.0})]
    engine = _engine(connector, responses)
    ctx = _ctx(engine, ["fetch_order", "write_api"])
    driver = LangGraphLoopDriver(checkpointer=MemorySaver(), interrupt_mode=True)

    outcome = driver.drive(ctx)

    assert driver.is_suspended(ctx.state.trace_id), "the graph must be parked, not finished"
    assert outcome.status is RunStatus.PENDING_HUMAN_APPROVAL
    assert driver.pending_interrupt(ctx.state.trace_id) is not None


def test_the_interrupt_payload_describes_the_pending_call(connector) -> None:
    responses = [_tool_response("write_api", {"order_id": "ORD-1001", "amount": 1200.0})]
    engine = _engine(connector, responses)
    ctx = _ctx(engine, ["fetch_order", "write_api"])
    driver = LangGraphLoopDriver(checkpointer=MemorySaver(), interrupt_mode=True)
    driver.drive(ctx)

    payload = driver.pending_interrupt(ctx.state.trace_id)
    assert payload is not None
    assert payload["tool_name"] == "write_api"
    assert payload["trace_id"] == ctx.state.trace_id
    assert payload["arguments"]["amount"] == 1200.0


def test_the_tool_has_not_run_while_suspended(connector) -> None:
    """Approval is what authorises execution; a parked run must not have run it."""
    responses = [_tool_response("write_api", {"order_id": "ORD-1001", "amount": 1200.0})]
    engine = _engine(connector, responses)
    ctx = _ctx(engine, ["fetch_order", "write_api"])
    driver = LangGraphLoopDriver(checkpointer=MemorySaver(), interrupt_mode=True)
    driver.drive(ctx)

    assert connector.call_count() == 0, "the high-risk tool ran before approval"
    assert ctx.state.execution_result is None


def test_no_interrupt_without_interrupt_mode(connector) -> None:
    """The default path still parks by returning, without touching the checkpoint."""
    responses = [_tool_response("write_api", {"order_id": "ORD-1001", "amount": 1200.0})]
    engine = _engine(connector, responses)
    ctx = _ctx(engine, ["fetch_order", "write_api"])
    driver = LangGraphLoopDriver(checkpointer=MemorySaver())
    outcome = driver.drive(ctx)
    assert outcome.status is RunStatus.PENDING_HUMAN_APPROVAL
    assert not driver.is_suspended(ctx.state.trace_id)


# ---------------------------------------------------------------------------
# approval
# ---------------------------------------------------------------------------


def test_approval_runs_the_tool_exactly_once(connector) -> None:
    responses = [
        _tool_response("write_api", {"order_id": "ORD-1001", "amount": 1200.0}),
        LLMResponse(content=dict(VALID)),
    ]
    engine = _engine(connector, responses)
    ctx = _ctx(engine, ["fetch_order", "write_api"])
    driver = LangGraphLoopDriver(checkpointer=MemorySaver(), interrupt_mode=True)

    driver.drive(ctx)
    assert connector.call_count() == 0

    outcome = driver.resume(ctx, "approve")

    assert connector.call_count() == 1, "the approved tool must run exactly once"
    assert not driver.is_suspended(ctx.state.trace_id)
    assert outcome.status in (RunStatus.RUNNING, RunStatus.FIRST_PASS, RunStatus.SELF_HEALED)


def test_approval_records_the_decision(connector) -> None:
    responses = [
        _tool_response("write_api", {"order_id": "ORD-1001", "amount": 1200.0}),
        LLMResponse(content=dict(VALID)),
    ]
    engine = _engine(connector, responses)
    ctx = _ctx(engine, ["fetch_order", "write_api"])
    driver = LangGraphLoopDriver(checkpointer=MemorySaver(), interrupt_mode=True)
    driver.drive(ctx)
    driver.resume(ctx, {"decision": "approve", "reason": "checked the amount"})

    assert ctx.state.approval_decision == "approve"
    assert ctx.state.approval_reason == "checked the amount"


def test_approval_does_not_re_run_the_earlier_steps(connector) -> None:
    """Nothing before the suspension may re-execute."""
    responses = [
        _tool_response("fetch_order", {"order_id": "ORD-1001"}),
        _tool_response("write_api", {"order_id": "ORD-1001", "amount": 1200.0}),
        LLMResponse(content=dict(VALID)),
    ]
    engine = _engine(connector, responses)
    ctx = _ctx(engine, ["fetch_order", "write_api"])
    driver = LangGraphLoopDriver(checkpointer=MemorySaver(), interrupt_mode=True)

    driver.drive(ctx)
    fetch_calls_before = connector.call_count()
    assert fetch_calls_before == 1, "fetch_order should have run once before the park"

    driver.resume(ctx, "approve")

    # fetch_order must not run again; only write_api is added.
    assert connector.call_count() == 2, connector.called_tools()


# ---------------------------------------------------------------------------
# rejection
# ---------------------------------------------------------------------------


def test_rejection_ends_the_run(connector) -> None:
    responses = [_tool_response("write_api", {"order_id": "ORD-1001", "amount": 1200.0})]
    engine = _engine(connector, responses)
    ctx = _ctx(engine, ["fetch_order", "write_api"])
    driver = LangGraphLoopDriver(checkpointer=MemorySaver(), interrupt_mode=True)
    driver.drive(ctx)

    outcome = driver.resume(ctx, "reject")

    assert outcome.status is RunStatus.REJECTED_BY_HUMAN
    assert connector.call_count() == 0, "a rejected tool must never run"
    assert not driver.is_suspended(ctx.state.trace_id)


def test_rejection_reason_reaches_the_state(connector) -> None:
    responses = [_tool_response("write_api", {"order_id": "ORD-1001", "amount": 1200.0})]
    engine = _engine(connector, responses)
    ctx = _ctx(engine, ["fetch_order", "write_api"])
    driver = LangGraphLoopDriver(checkpointer=MemorySaver(), interrupt_mode=True)
    driver.drive(ctx)
    driver.resume(ctx, {"decision": "reject", "reason": "amount looks wrong"})

    assert ctx.state.approval_decision == "reject"
    assert "amount looks wrong" in (ctx.state.failure_reason or "")


def test_a_noncommittal_answer_is_a_rejection(connector) -> None:
    responses = [_tool_response("write_api", {"order_id": "ORD-1001", "amount": 1200.0})]
    engine = _engine(connector, responses)
    ctx = _ctx(engine, ["fetch_order", "write_api"])
    driver = LangGraphLoopDriver(checkpointer=MemorySaver(), interrupt_mode=True)
    driver.drive(ctx)
    outcome = driver.resume(ctx, "maybe later")
    assert outcome.status is RunStatus.REJECTED_BY_HUMAN
    assert connector.call_count() == 0


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------


def test_resume_without_interrupt_mode_is_refused(connector) -> None:
    engine = _engine(connector, [])
    ctx = _ctx(engine, ["fetch_order"])
    driver = LangGraphLoopDriver(checkpointer=MemorySaver())
    with pytest.raises(RuntimeError, match="interrupt_mode"):
        driver.resume(ctx, "approve")


def test_is_suspended_is_false_for_an_unknown_thread(connector) -> None:
    driver = LangGraphLoopDriver(checkpointer=MemorySaver(), interrupt_mode=True)
    assert driver.is_suspended("no-such-thread") is False


def test_pending_interrupt_is_none_for_a_finished_run(connector) -> None:
    responses = [LLMResponse(content=dict(VALID))]
    engine = _engine(connector, responses)
    ctx = _ctx(engine, ["fetch_order"])
    driver = LangGraphLoopDriver(checkpointer=MemorySaver(), interrupt_mode=True)
    driver.drive(ctx)
    assert driver.pending_interrupt(ctx.state.trace_id) is None


# ---------------------------------------------------------------------------
# a timeout is not a pending decision
# ---------------------------------------------------------------------------


def test_a_gateway_timeout_is_not_treated_as_awaiting_a_decision(connector) -> None:
    """``timeout`` means "nobody answered" — resuming on it would invent a decision.

    The engine records the gateway's ``timeout`` decision and leaves the run parked.
    If ``_awaiting_decision`` ignored the decision field, the next ``resume()`` would
    treat that run as still-waiting and answer it, turning silence into a verdict.
    """
    from harness_core.hitl import FakeWebhookNotifier, HumanApprovalGateway
    from harness_core.loop_langgraph import _awaiting_decision

    # The default handler resolves to "timeout": nobody answered.
    gateway = HumanApprovalGateway(FakeWebhookNotifier())
    responses = [_tool_response("write_api", {"order_id": "ORD-1001", "amount": 1200.0})]
    engine = _engine(connector, responses, gateway=gateway)
    ctx = _ctx(engine, ["fetch_order", "write_api"])

    driver = LangGraphLoopDriver(checkpointer=MemorySaver(), interrupt_mode=True)
    outcome = driver.drive(ctx)

    assert outcome.status is RunStatus.PENDING_HUMAN_APPROVAL
    assert ctx.state.approval_decision == "timeout"
    assert _awaiting_decision(ctx.state) is False, (
        "a timed-out gateway is not a pending decision"
    )
    assert connector.call_count() == 0


def test_a_run_with_no_decision_is_awaiting_one(connector) -> None:
    """Control for the test above: a genuinely parked run *is* awaiting a decision."""
    from harness_core.loop_langgraph import _awaiting_decision

    responses = [_tool_response("write_api", {"order_id": "ORD-1001", "amount": 1200.0})]
    engine = _engine(connector, responses)  # no gateway -> decision stays None
    ctx = _ctx(engine, ["fetch_order", "write_api"])
    driver = LangGraphLoopDriver(checkpointer=MemorySaver(), interrupt_mode=True)
    driver.drive(ctx)

    assert ctx.state.approval_decision is None
    assert _awaiting_decision(ctx.state) is True
