"""Human-in-the-loop gateway for high-risk tool calls.

The rule this module enforces: **a model's tool arguments never constitute an
approval**. A high-risk call is suspended, a notifier is asked to reach a human,
and the run only continues when an explicit decision comes back.

``FakeWebhookNotifier`` records every notification in memory so tests and demos
can assert on it without any network access.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

from .state import ToolCall

Decision = Literal["approve", "reject", "timeout"]


@dataclass
class ApprovalRequest:
    """What the human is asked to approve."""

    request_id: str
    trace_id: str
    tool_name: str
    arguments: dict[str, Any]
    task_prompt: str = ""
    reason: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "task_prompt": self.task_prompt,
            "reason": self.reason,
            "created_at": self.created_at,
        }

    def to_message(self) -> str:
        return (
            f"[HITL] approval required for high-risk tool {self.tool_name!r} "
            f"(trace={self.trace_id}, request={self.request_id})\n"
            f"arguments: {json.dumps(self.arguments, ensure_ascii=False)}\n"
            f"reason: {self.reason or 'high-risk tool'}"
        )


@dataclass
class ApprovalResponse:
    request_id: str
    decision: Decision
    reason: str = ""

    @property
    def approved(self) -> bool:
        return self.decision == "approve"


class WebhookNotifier(Protocol):
    """Transport for approval requests (Slack / Teams / HTTP / pager)."""

    def notify(self, request: ApprovalRequest) -> None: ...


class FakeWebhookNotifier:
    """In-memory notifier: records requests, never touches the network."""

    def __init__(self) -> None:
        self.requests: list[ApprovalRequest] = []

    def notify(self, request: ApprovalRequest) -> None:
        self.requests.append(request)

    @property
    def count(self) -> int:
        return len(self.requests)

    @property
    def last(self) -> ApprovalRequest | None:
        return self.requests[-1] if self.requests else None

    def clear(self) -> None:
        self.requests.clear()


class HttpWebhookNotifier:
    """Real HTTP webhook (Slack-compatible ``{"text": ...}`` payload).

    Off by default and never exercised in tests: the demo and CI must stay
    offline. ``urllib`` is imported **inside** ``notify`` so that merely importing
    the package never pulls in ``ssl``/``socket`` — that is what lets the offline
    audit block sockets outright.
    """

    def __init__(self, url: str, *, timeout: float = 5.0) -> None:
        self.url = url
        self.timeout = timeout

    def notify(self, request: ApprovalRequest) -> None:
        import urllib.error
        import urllib.request

        body = json.dumps({"text": request.to_message()}).encode("utf-8")
        http_request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout) as response:
                response.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            # A failed notification must not crash the run: the harness degrades to
            # "approval pending" and the caller decides what to do.
            raise NotificationError(f"webhook notification failed: {exc}") from exc


class NotificationError(RuntimeError):
    """Raised when an approval request could not be delivered."""


ApprovalHandler = Callable[[ApprovalRequest], ApprovalResponse]


class HumanApprovalGateway:
    """Suspends high-risk calls and resolves them into a decision.

    Parameters
    ----------
    notifier:
        Where to send the approval request.
    handler:
        Optional programmatic responder (demos, tests, "auto-approve" policies).
        When omitted the gateway returns ``timeout`` — i.e. the run stays pending
        instead of silently proceeding, which is the safe default.
    default_decision:
        Decision used when no handler answers. Defaults to ``timeout``.
    """

    def __init__(
        self,
        notifier: WebhookNotifier | None = None,
        handler: ApprovalHandler | None = None,
        *,
        default_decision: Decision = "timeout",
    ) -> None:
        self.notifier = notifier or FakeWebhookNotifier()
        self.handler = handler
        self.default_decision = default_decision
        self.pending: dict[str, ApprovalRequest] = {}
        self.responses: dict[str, ApprovalResponse] = {}
        self.notification_errors: list[str] = []

    # ------------------------------------------------------------------ API
    def request_approval(
        self,
        call: ToolCall,
        *,
        trace_id: str,
        task_prompt: str = "",
        reason: str = "",
    ) -> ApprovalResponse:
        request = ApprovalRequest(
            request_id=uuid.uuid4().hex[:12],
            trace_id=trace_id,
            tool_name=call.tool_name,
            arguments=dict(call.arguments),
            task_prompt=task_prompt,
            reason=reason or "high-risk tool requires human approval",
        )
        self.pending[request.request_id] = request

        try:
            self.notifier.notify(request)
        except NotificationError as exc:
            self.notification_errors.append(str(exc))

        response = self._resolve(request)
        self.responses[request.request_id] = response
        self.pending.pop(request.request_id, None)
        return response

    def approve(self, request_id: str, reason: str = "approved by human") -> ApprovalResponse:
        """Simulate an out-of-band approval (demo / test helper)."""
        response = ApprovalResponse(request_id=request_id, decision="approve", reason=reason)
        self.responses[request_id] = response
        self.pending.pop(request_id, None)
        return response

    def reject(self, request_id: str, reason: str = "rejected by human") -> ApprovalResponse:
        response = ApprovalResponse(request_id=request_id, decision="reject", reason=reason)
        self.responses[request_id] = response
        self.pending.pop(request_id, None)
        return response

    # -------------------------------------------------------------- internal
    def _resolve(self, request: ApprovalRequest) -> ApprovalResponse:
        if self.handler is not None:
            response = self.handler(request)
            return response
        return ApprovalResponse(
            request_id=request.request_id,
            decision=self.default_decision,
            reason=(
                "no approval handler configured; run stays PENDING_HUMAN_APPROVAL"
                if self.default_decision == "timeout"
                else f"default decision: {self.default_decision}"
            ),
        )


class AutoApprover:
    """Handler that approves or rejects every request (for demos / tests)."""

    def __init__(self, decision: Decision = "approve", reason: str = "") -> None:
        self.decision = decision
        self.reason = reason or f"auto-{decision}"

    def __call__(self, request: ApprovalRequest) -> ApprovalResponse:
        return ApprovalResponse(
            request_id=request.request_id,
            decision=self.decision,
            reason=self.reason,
        )


class RecordingApprover:
    """Handler that approves the first N requests, then rejects."""

    def __init__(self, approve_first: int = 1) -> None:
        self.approve_first = approve_first
        self.seen: list[ApprovalRequest] = []

    def __call__(self, request: ApprovalRequest) -> ApprovalResponse:
        self.seen.append(request)
        if len(self.seen) <= self.approve_first:
            return ApprovalResponse(request.request_id, "approve", "within approve quota")
        return ApprovalResponse(request.request_id, "reject", "quota exceeded")
