"""HITL tests: suspension, webhook notification, approve / reject / resume."""

from __future__ import annotations

import pytest

from harness_core.hitl import (
    ApprovalRequest,
    AutoApprover,
    FakeWebhookNotifier,
    HumanApprovalGateway,
    RecordingApprover,
)
from harness_core.state import RunStatus

from .helpers import VALID, response_for, tool_response


def _write_responses():
    return [
        tool_response("write_api", {"order_id": "ORD-1001", "amount": 1200.0}),
        response_for(),
    ]


# ---------------------------------------------------------------------------
# gateway unit behaviour
# ---------------------------------------------------------------------------


def test_gateway_records_notification_and_defaults_to_pending(webhook: FakeWebhookNotifier) -> None:
    from harness_core.state import ToolCall

    gateway = HumanApprovalGateway(webhook)
    response = gateway.request_approval(
        ToolCall("write_api", {"order_id": "ORD-1", "amount": 1}),
        trace_id="trace-1",
        task_prompt="settle",
    )
    assert webhook.count == 1
    assert response.decision == "timeout"
    assert webhook.last is not None and webhook.last.trace_id == "trace-1"
    assert "write_api" in webhook.last.to_message()


def test_gateway_approve_and_reject_helpers(webhook: FakeWebhookNotifier) -> None:
    from harness_core.state import ToolCall

    gateway = HumanApprovalGateway(webhook)
    call = ToolCall("write_api", {"order_id": "ORD-1", "amount": 1})
    first = gateway.request_approval(call, trace_id="t1")
    approved = gateway.approve(first.request_id, "ok")
    assert approved.approved
    assert first.request_id in gateway.responses

    second = gateway.request_approval(call, trace_id="t2")
    rejected = gateway.reject(second.request_id, "no")
    assert rejected.decision == "reject"
    assert gateway.pending == {}


def test_auto_approver_and_recording_approver() -> None:
    request = ApprovalRequest("r1", "t1", "write_api", {})
    assert AutoApprover("approve")(request).approved
    assert AutoApprover("reject")(request).decision == "reject"

    recorder = RecordingApprover(approve_first=1)
    assert recorder(request).approved
    assert recorder(request).decision == "reject"


def test_notification_failure_does_not_crash_the_run(webhook: FakeWebhookNotifier) -> None:
    from harness_core.hitl import NotificationError
    from harness_core.state import ToolCall

    class BrokenNotifier:
        def notify(self, request: ApprovalRequest) -> None:
            raise NotificationError("network down")

    gateway = HumanApprovalGateway(BrokenNotifier(), handler=AutoApprover("approve"))
    response = gateway.request_approval(ToolCall("write_api", {}), trace_id="t")
    assert response.approved
    assert gateway.notification_errors


# ---------------------------------------------------------------------------
# engine integration
# ---------------------------------------------------------------------------


def test_high_risk_call_suspends_and_does_not_execute(engine_factory, webhook) -> None:
    gateway = HumanApprovalGateway(webhook)  # no handler -> pending
    engine, _ = engine_factory(_write_responses(), gateway=gateway)
    result = engine.run("settle", tool_whitelist=["fetch_order", "write_api"])

    assert result.status is RunStatus.PENDING_HUMAN_APPROVAL
    assert result.exit_code == 1
    assert engine.connector.call_count("write_api") == 0  # type: ignore[attr-defined]
    assert webhook.count == 1
    assert result.approval_request_id


def test_approval_executes_the_tool_once(engine_factory, webhook) -> None:
    gateway = HumanApprovalGateway(webhook, handler=AutoApprover("approve"))
    engine, _ = engine_factory(_write_responses(), gateway=gateway)
    result = engine.run("settle", tool_whitelist=["fetch_order", "write_api"])

    assert result.ok
    assert engine.connector.call_count("write_api") == 1  # type: ignore[attr-defined]
    assert result.state is not None
    assert result.state.approval_decision == "approve"


def test_rejection_stops_the_run_without_executing(engine_factory, webhook) -> None:
    gateway = HumanApprovalGateway(webhook, handler=AutoApprover("reject", "policy"))
    engine, _ = engine_factory(_write_responses(), gateway=gateway)
    result = engine.run("settle", tool_whitelist=["fetch_order", "write_api"])

    assert result.status is RunStatus.REJECTED_BY_HUMAN
    assert result.exit_code == 1
    assert engine.connector.call_count("write_api") == 0  # type: ignore[attr-defined]
    assert "policy" in (result.failure_reason or "")


def test_resume_after_out_of_band_approval(engine_factory, webhook) -> None:
    gateway = HumanApprovalGateway(webhook)
    engine, _ = engine_factory(_write_responses(), gateway=gateway)
    result = engine.run("settle", tool_whitelist=["fetch_order", "write_api"])
    assert result.status is RunStatus.PENDING_HUMAN_APPROVAL

    gateway.approve(result.approval_request_id or "", "reviewed")
    resumed = engine.resume(result)

    assert resumed.ok
    assert resumed.trace_id == result.trace_id
    assert engine.connector.call_count("write_api") == 1  # type: ignore[attr-defined]


def test_resume_after_rejection(engine_factory, webhook) -> None:
    gateway = HumanApprovalGateway(webhook)
    engine, _ = engine_factory(_write_responses(), gateway=gateway)
    result = engine.run("settle", tool_whitelist=["fetch_order", "write_api"])

    gateway.reject(result.approval_request_id or "", "no way")
    resumed = engine.resume(result)

    assert resumed.status is RunStatus.REJECTED_BY_HUMAN
    assert engine.connector.call_count("write_api") == 0  # type: ignore[attr-defined]


def test_resume_requires_a_suspended_run(engine_factory) -> None:
    engine, _ = engine_factory([response_for()])
    result = engine.run("settle", tool_whitelist=["fetch_order"])
    with pytest.raises(ValueError, match="PENDING_HUMAN_APPROVAL"):
        engine.resume(result)


def test_resume_requires_a_recorded_decision(engine_factory, webhook) -> None:
    gateway = HumanApprovalGateway(webhook)
    engine, _ = engine_factory(_write_responses(), gateway=gateway)
    result = engine.run("settle", tool_whitelist=["fetch_order", "write_api"])
    with pytest.raises(ValueError, match="no recorded human decision"):
        engine.resume(result)


def test_missing_gateway_parks_the_run(engine_factory) -> None:
    engine, _ = engine_factory(_write_responses(), gateway=None)
    result = engine.run("settle", tool_whitelist=["fetch_order", "write_api"])
    assert result.status is RunStatus.PENDING_HUMAN_APPROVAL
    assert "no HumanApprovalGateway" in (result.failure_reason or "")


def test_approval_does_not_bypass_validation(engine_factory, webhook) -> None:
    """Approval authorises execution; the output contract still applies."""
    bad_payload = {**VALID, "currency": "EUR"}
    gateway = HumanApprovalGateway(webhook, handler=AutoApprover("approve"))
    engine, llm = engine_factory(
        [tool_response("write_api", {"order_id": "ORD-1001", "amount": 1200.0}), response_for(bad_payload), response_for()],
        gateway=gateway,
    )
    result = engine.run("settle", tool_whitelist=["fetch_order", "write_api"])
    assert result.status is RunStatus.SELF_HEALED
    assert "output.schema.enum" in llm.prompts[2]
