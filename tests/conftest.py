"""Shared fixtures for the harness test-suite.

All fixtures are offline: scripted LLM, in-process connector, fake webhook.
The data builders live in :mod:`tests.helpers` so non-fixture code can reuse them.
"""

from __future__ import annotations

import pytest

from harness_core.adapters.llm import FakeLLMAdapter, LLMResponse
from harness_core.adapters.mcp import FakeMCPConnector
from harness_core.engine import HarnessOrchestrationEngine
from harness_core.guards import BusinessRule, OutputGuard, ToolSpec
from harness_core.hitl import FakeWebhookNotifier, HumanApprovalGateway
from harness_core.observability import MemorySink, StructuredLogger, Tracer

from .helpers import DELETE_SCHEMA, FETCH_SCHEMA, ORDER_SCHEMA, VALID, WRITE_SCHEMA  # noqa: F401


@pytest.fixture()
def connector() -> FakeMCPConnector:
    fake = FakeMCPConnector()
    fake.add_tool(
        "fetch_order",
        lambda args: {"order_id": args["order_id"], "subtotal": 1200.0, "currency": "CNY"},
        input_schema=FETCH_SCHEMA,
    )
    fake.add_tool(
        "write_api",
        lambda args: {"written": True, "order_id": args["order_id"], "amount": args["amount"]},
        input_schema=WRITE_SCHEMA,
    )
    fake.add_tool("delete_resource", lambda args: {"deleted": args["id"]}, input_schema=DELETE_SCHEMA)
    return fake


@pytest.fixture()
def specs() -> dict[str, ToolSpec]:
    return {
        "fetch_order": ToolSpec("fetch_order", "read", FETCH_SCHEMA),
        "write_api": ToolSpec("write_api", "write", WRITE_SCHEMA, high_risk=True),
        "delete_resource": ToolSpec("delete_resource", "delete", DELETE_SCHEMA, high_risk=True),
    }


@pytest.fixture()
def guard() -> OutputGuard:
    return OutputGuard(
        output_schema=ORDER_SCHEMA,
        forbidden_strings=["TODO"],
        rules=[
            BusinessRule(
                name="amount_positive", op="gt", field="amount", value=0,
                message="结算金额必须大于 0",
            )
        ],
    )


@pytest.fixture()
def sink() -> MemorySink:
    return MemorySink()


@pytest.fixture()
def engine_factory(connector: FakeMCPConnector, specs: dict[str, ToolSpec], guard: OutputGuard, sink: MemorySink):
    def make(
        responses: list[LLMResponse] | None = None,
        *,
        responder=None,
        gateway: HumanApprovalGateway | None = None,
        max_retries: int = 3,
        max_steps: int = 8,
        output_guard: OutputGuard | None = None,
        llm: FakeLLMAdapter | None = None,
    ) -> tuple[HarnessOrchestrationEngine, FakeLLMAdapter]:
        fake_llm = llm or FakeLLMAdapter(responses or [], responder=responder)
        engine = HarnessOrchestrationEngine(
            llm=fake_llm,
            connector=connector,
            output_guard=output_guard or guard,
            approval_gateway=gateway,
            tool_specs=specs,
            high_risk_tools={"write_api", "delete_resource"},
            max_retries=max_retries,
            max_steps=max_steps,
            tracer=Tracer(sink=sink),
            logger=StructuredLogger(enabled=False),
        )
        return engine, fake_llm

    return make


@pytest.fixture()
def webhook() -> FakeWebhookNotifier:
    return FakeWebhookNotifier()


__all__ = ["VALID", "connector", "specs", "guard", "sink", "engine_factory", "webhook"]
