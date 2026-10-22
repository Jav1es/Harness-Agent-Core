"""Adapter tests: scripted LLM, in-process MCP connector, protocol conformance."""

from __future__ import annotations

import pytest

from harness_core.adapters.llm import (
    FakeLLMAdapter,
    LLMAdapter,
    LLMResponse,
    ToolCallRequest,
    json_response,
)
from harness_core.adapters.mcp import (
    FakeMCPConnector,
    MCPConnector,
    StdioMCPConnector,
    ToolDefinition,
    ToolResult,
)
from harness_core.state import AgentHarnessState


def _state() -> AgentHarnessState:
    return AgentHarnessState(task_prompt="t", tool_whitelist=[])


# ---------------------------------------------------------------------------
# LLM adapter
# ---------------------------------------------------------------------------


def test_fake_llm_returns_scripted_responses_in_order() -> None:
    llm = FakeLLMAdapter(
        [
            LLMResponse(content={"n": 1}, tokens_estimate=5),
            LLMResponse(content={"n": 2}, tokens_estimate=5),
        ]
    )
    state = _state()
    assert llm.generate("p", state).content == {"n": 1}
    assert llm.generate("p", state).content == {"n": 2}
    assert llm.prompt_count == 2


def test_fake_llm_repeats_the_last_response_by_default() -> None:
    llm = FakeLLMAdapter([LLMResponse(content={"n": 1})])
    state = _state()
    llm.generate("p", state)
    assert llm.generate("p", state).content == {"n": 1}


def test_fake_llm_can_exhaust_its_script() -> None:
    llm = FakeLLMAdapter([LLMResponse(content={"n": 1})], repeat_last=False)
    state = _state()
    llm.generate("p", state)
    exhausted = llm.generate("p", state)
    assert exhausted.error is not None
    assert "script exhausted" in exhausted.error


def test_fake_llm_without_script_reports_an_error() -> None:
    response = FakeLLMAdapter([]).generate("p", _state())
    assert response.error is not None


def test_fake_llm_responder_receives_the_attempt_index() -> None:
    seen: list[int] = []

    def responder(prompt: str, state: AgentHarnessState, attempt: int) -> LLMResponse:
        seen.append(attempt)
        return LLMResponse(content={"attempt": attempt})

    llm = FakeLLMAdapter(responder=responder)
    state = _state()
    for _ in range(3):
        llm.generate("p", state)
    assert seen == [0, 1, 2]


def test_fake_llm_records_prompts() -> None:
    llm = FakeLLMAdapter([LLMResponse(content={})])
    llm.generate("first", _state())
    llm.generate("second", _state())
    assert llm.prompts == ["first", "second"]
    assert llm.last_prompt() == "second"


def test_json_response_helper_builds_a_tool_call() -> None:
    response = json_response(tool_name="fetch_order", arguments={"order_id": "ORD-1"})
    assert response.tool_call is not None
    assert response.tool_call.tool_name == "fetch_order"
    assert response.tool_call.arguments == {"order_id": "ORD-1"}


def test_tool_call_request_roundtrip() -> None:
    request = ToolCallRequest("write_api", {"amount": 1})
    assert request.to_dict() == {"tool_name": "write_api", "arguments": {"amount": 1}}


def test_fake_llm_satisfies_the_protocol() -> None:
    assert isinstance(FakeLLMAdapter([]), LLMAdapter)


# ---------------------------------------------------------------------------
# MCP connector
# ---------------------------------------------------------------------------


def test_fake_connector_lists_registered_tools() -> None:
    connector = FakeMCPConnector()
    connector.add_tool("a", lambda args: {"ok": True}, description="A")
    connector.add_tool("b", lambda args: {"ok": True})
    names = [tool.name for tool in connector.list_tools()]
    assert names == ["a", "b"]
    assert connector.list_tools()[0].description == "A"


def test_fake_connector_executes_a_handler() -> None:
    connector = FakeMCPConnector({"echo": lambda args: {"echo": args}})
    result = connector.call_tool("echo", {"x": 1})
    assert result.ok is True
    assert result.content == {"echo": {"x": 1}}
    assert result.duration_ms >= 0


def test_fake_connector_unknown_tool_is_a_failed_result() -> None:
    result = FakeMCPConnector().call_tool("nope", {})
    assert result.ok is False
    assert "not implemented" in (result.error or "")


def test_fake_connector_captures_handler_exceptions() -> None:
    def explode(_args):
        raise ValueError("bad input")

    connector = FakeMCPConnector({"boom": explode})
    result = connector.call_tool("boom", {})
    assert result.ok is False
    assert "ValueError: bad input" in (result.error or "")
    assert "traceback" in result.meta


def test_fake_connector_records_calls() -> None:
    connector = FakeMCPConnector({"a": lambda args: 1, "b": lambda args: 2})
    connector.call_tool("a", {})
    connector.call_tool("b", {})
    connector.call_tool("a", {})
    assert connector.call_count() == 3
    assert connector.call_count("a") == 2
    assert connector.called_tools() == ["a", "b", "a"]


def test_fake_connector_close() -> None:
    connector = FakeMCPConnector()
    connector.close()
    assert connector.closed is True


def test_tool_definition_roundtrip() -> None:
    definition = ToolDefinition("x", "desc", {"type": "object"})
    assert definition.to_dict() == {
        "name": "x",
        "description": "desc",
        "input_schema": {"type": "object"},
    }


def test_tool_result_roundtrip() -> None:
    result = ToolResult("x", True, {"a": 1}, duration_ms=1.23456)
    payload = result.to_dict()
    assert payload["duration_ms"] == 1.235


def test_fake_connector_satisfies_the_protocol() -> None:
    assert isinstance(FakeMCPConnector(), MCPConnector)


def test_stdio_connector_requires_the_optional_extra() -> None:
    """The skeleton must fail loudly, not silently pretend to work."""
    connector = StdioMCPConnector("some-mcp-server")
    with pytest.raises((RuntimeError, NotImplementedError)):
        connector.list_tools()


def test_stdio_connector_close_is_safe() -> None:
    StdioMCPConnector("x").close()


# ---------------------------------------------------------------------------
# no-network guarantee
# ---------------------------------------------------------------------------


def test_adapters_never_touch_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake adapters must not open sockets; this is what keeps CI offline."""

    import socket

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the offline adapters must not create sockets")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    connector = FakeMCPConnector({"echo": lambda args: args})
    assert connector.call_tool("echo", {"a": 1}).ok
    llm = FakeLLMAdapter([LLMResponse(content={"ok": True})])
    assert llm.generate("p", _state()).content == {"ok": True}
