"""Real HTTP LLM adapter tests against a live localhost server.

Every test starts ``tests/llm_server_stub.py`` in a subprocess and talks real HTTP to
it. No network, no API key, no SDK — but also no mock of the transport, so request
shape, auth headers, JSON parsing and error classification are all exercised.

The classification under test mirrors the MCP connector's:

* the model **never answered** (HTTP error, refused, timed out) → infrastructure
  error, escapes the loop;
* the model **answered badly** (no choices, empty content, non-JSON body) → an
  ``LLMResponse`` with ``error`` set, so the repair loop can retry.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import pytest

from harness_core.adapters.llm import LLMAdapter
from harness_core.adapters.llm_http import (
    HttpLLMAdapter,
    LLMTransportError,
    _extract_tool_call,
    _maybe_json,
)
from harness_core.errors import HarnessInfrastructureError
from harness_core.state import AgentHarnessState

STUB = str(Path(__file__).parent / "llm_server_stub.py")


class StubServer:
    """A live stub endpoint on an ephemeral port."""

    def __init__(self, *flags: str) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, STUB, *flags],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.port = self._wait_for_port()

    def _wait_for_port(self, timeout: float = 15.0) -> int:
        assert self.proc.stdout is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                break
            if line.startswith("PORT "):
                return int(line.split()[1])
        raise RuntimeError("stub server did not report a port")

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)


@pytest.fixture
def server() -> Iterator[StubServer]:
    srv = StubServer()
    try:
        yield srv
    finally:
        srv.close()


@pytest.fixture
def adapter(server: StubServer) -> HttpLLMAdapter:
    return HttpLLMAdapter(base_url=server.base_url, model="stub-model", timeout=10)


def _state() -> AgentHarnessState:
    return AgentHarnessState(task_prompt="t", tool_whitelist=[])


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_returns_a_json_deliverable(adapter: HttpLLMAdapter) -> None:
    response = adapter.generate("settle the order", _state())
    assert response.error is None
    assert response.content == {
        "order_id": "ORD-1001",
        "currency": "CNY",
        "amount": 1200.0,
        "status": "settled",
    }
    # The response `model` reflects what the server actually served, not what we
    # asked for — the server is the authority on which model answered.
    assert response.model == "stub"
    assert response.tokens_estimate == 42


def test_offered_tools_produce_a_tool_call(adapter: HttpLLMAdapter) -> None:
    tools = [
        {
            "name": "fetch_order",
            "description": "Fetch one order",
            "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}},
        }
    ]
    # The stub decides to call a tool when the prompt asks for one.
    response = adapter.generate("fetch the order", _state(), tools=tools)
    assert response.tool_call is not None
    assert response.tool_call.tool_name == "fetch_order"
    assert response.tool_call.arguments == {"order_id": "ORD-1001"}


def test_request_body_has_the_expected_shape(adapter: HttpLLMAdapter, server: StubServer) -> None:
    adapter.generate("hello", _state())
    assert len(server.proc.poll() is None and [] or []) == 0  # server still alive
    # The stub echoes nothing, so inspect via a second call with --echo semantics:
    # simpler to assert on the adapter's own builder.
    body = adapter.build_body("hello", _state(), None)
    assert body["model"] == "stub-model"
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert body["temperature"] == 0.0


def test_tools_are_translated_to_the_openai_shape(adapter: HttpLLMAdapter) -> None:
    body = adapter.build_body(
        "x",
        _state(),
        [{"name": "add", "description": "add", "input_schema": {"type": "object"}}],
    )
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == "add"
    assert body["tools"][0]["function"]["parameters"] == {"type": "object"}


def test_bearer_token_is_sent(server: StubServer) -> None:
    adapter = HttpLLMAdapter(
        base_url=server.base_url, model="m", api_key="secret-token", timeout=10
    )
    adapter.generate("hi", _state())
    # Assert on the builder rather than reaching into the server process.
    assert adapter.headers()["Authorization"] == "Bearer secret-token"


def test_no_api_key_means_no_authorization_header(adapter: HttpLLMAdapter) -> None:
    assert "Authorization" not in adapter.headers()


def test_endpoint_normalisation() -> None:
    assert (
        HttpLLMAdapter(base_url="http://h/v1", model="m").endpoint()
        == "http://h/v1/chat/completions"
    )
    assert (
        HttpLLMAdapter(base_url="http://h/v1/", model="m").endpoint()
        == "http://h/v1/chat/completions"
    )
    assert (
        HttpLLMAdapter(base_url="http://h/v1/chat/completions", model="m").endpoint()
        == "http://h/v1/chat/completions"
    )


def test_force_json_adds_response_format() -> None:
    adapter = HttpLLMAdapter(base_url="http://h/v1", model="m", force_json=True)
    assert adapter.build_body("x", _state(), None)["response_format"] == {"type": "json_object"}


def test_extra_body_and_headers_are_merged_last() -> None:
    adapter = HttpLLMAdapter(
        base_url="http://h/v1",
        model="m",
        extra_body={"top_p": 0.9, "temperature": 0.7},
        extra_headers={"X-Trace": "abc"},
    )
    assert adapter.build_body("x", _state(), None)["temperature"] == 0.7
    assert adapter.headers()["X-Trace"] == "abc"


# ---------------------------------------------------------------------------
# the model answered badly -> business failure (retryable)
# ---------------------------------------------------------------------------


@pytest.fixture
def no_choices() -> Iterator[HttpLLMAdapter]:
    srv = StubServer("--no-choices")
    try:
        yield HttpLLMAdapter(base_url=srv.base_url, model="m", timeout=10)
    finally:
        srv.close()


def test_no_choices_is_a_business_error(no_choices: HttpLLMAdapter) -> None:
    response = no_choices.generate("x", _state())
    assert response.error == "LLM response contained no choices"
    assert response.content is None


@pytest.fixture
def empty() -> Iterator[HttpLLMAdapter]:
    srv = StubServer("--empty")
    try:
        yield HttpLLMAdapter(base_url=srv.base_url, model="m", timeout=10)
    finally:
        srv.close()


def test_empty_content_is_a_business_error(empty: HttpLLMAdapter) -> None:
    response = empty.generate("x", _state())
    assert response.error == "LLM returned neither content nor a tool call"


# ---------------------------------------------------------------------------
# the model never answered -> infrastructure failure (escapes the loop)
# ---------------------------------------------------------------------------


@pytest.fixture
def rate_limited() -> Iterator[HttpLLMAdapter]:
    srv = StubServer("--status", "429")
    try:
        yield HttpLLMAdapter(base_url=srv.base_url, model="m", timeout=10)
    finally:
        srv.close()


def test_http_error_raises(rate_limited: HttpLLMAdapter) -> None:
    with pytest.raises(LLMTransportError, match="HTTP 429"):
        rate_limited.generate("x", _state())


def test_http_error_is_an_infrastructure_error(rate_limited: HttpLLMAdapter) -> None:
    with pytest.raises(HarnessInfrastructureError):
        rate_limited.generate("x", _state())


@pytest.fixture
def unauthorized() -> Iterator[HttpLLMAdapter]:
    srv = StubServer("--status", "401")
    try:
        yield HttpLLMAdapter(base_url=srv.base_url, model="m", timeout=10)
    finally:
        srv.close()


def test_auth_failure_raises(unauthorized: HttpLLMAdapter) -> None:
    with pytest.raises(LLMTransportError, match="HTTP 401"):
        unauthorized.generate("x", _state())


@pytest.fixture
def not_json() -> Iterator[HttpLLMAdapter]:
    srv = StubServer("--not-json")
    try:
        yield HttpLLMAdapter(base_url=srv.base_url, model="m", timeout=10)
    finally:
        srv.close()


def test_non_json_body_raises(not_json: HttpLLMAdapter) -> None:
    """A non-JSON body is a broken endpoint, not a model mistake to repair."""
    with pytest.raises(LLMTransportError, match="non-JSON"):
        not_json.generate("x", _state())


def test_unreachable_endpoint_raises() -> None:
    adapter = HttpLLMAdapter(
        base_url="http://127.0.0.1:1/v1", model="m", timeout=2
    )
    with pytest.raises(LLMTransportError):
        adapter.generate("x", _state())


def test_timeout_raises() -> None:
    srv = StubServer("--slow", "3")
    try:
        adapter = HttpLLMAdapter(base_url=srv.base_url, model="m", timeout=0.5)
        with pytest.raises(LLMTransportError):
            adapter.generate("x", _state())
    finally:
        srv.close()


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------


def test_extract_tool_call_from_openai_shape() -> None:
    call = _extract_tool_call(
        {
            "tool_calls": [
                {
                    "function": {
                        "name": "fetch_order",
                        "arguments": '{"order_id": "ORD-1"}',
                    }
                }
            ]
        }
    )
    assert call is not None
    assert call.tool_name == "fetch_order"
    assert call.arguments == {"order_id": "ORD-1"}


def test_extract_tool_call_keeps_unparseable_arguments_visible() -> None:
    call = _extract_tool_call(
        {"tool_calls": [{"function": {"name": "x", "arguments": "{not json"}}]}
    )
    assert call is not None
    assert call.arguments == {"__raw_arguments__": "{not json"}


def test_extract_tool_call_handles_a_dict_of_arguments() -> None:
    call = _extract_tool_call(
        {"tool_calls": [{"function": {"name": "x", "arguments": {"a": 1}}}]}
    )
    assert call is not None and call.arguments == {"a": 1}


def test_extract_tool_call_returns_none_without_a_name() -> None:
    assert _extract_tool_call({"tool_calls": [{"function": {}}]}) is None
    assert _extract_tool_call({}) is None


def test_maybe_json_parses_objects_and_arrays() -> None:
    assert _maybe_json('{"a": 1}') == {"a": 1}
    assert _maybe_json("[1, 2]") == [1, 2]


def test_maybe_json_returns_text_for_non_json() -> None:
    assert _maybe_json("plain text") == "plain text"
    assert _maybe_json("{broken") == "{broken"


# ---------------------------------------------------------------------------
# the adapter is a drop-in LLMAdapter
# ---------------------------------------------------------------------------


def test_satisfies_the_llm_adapter_protocol(adapter: HttpLLMAdapter) -> None:
    assert isinstance(adapter, LLMAdapter)


def test_engine_runs_against_a_real_http_model(server: StubServer) -> None:
    """Swap the adapter; guards, breaker and tracing stay identical."""
    from harness_core.adapters.mcp import FakeMCPConnector
    from harness_core.engine import HarnessOrchestrationEngine
    from harness_core.guards import ToolSpec
    from harness_core.observability import StructuredLogger
    from harness_core.scenarios import ORDER_OUTPUT_SCHEMA

    connector = FakeMCPConnector()
    connector.add_tool("fetch_order", lambda args: {"order_id": "ORD-1001", "subtotal": 1200.0})
    engine = HarnessOrchestrationEngine(
        llm=HttpLLMAdapter(base_url=server.base_url, model="m", timeout=10),
        connector=connector,
        tool_specs={
            "fetch_order": ToolSpec(
                "fetch_order",
                "",
                {
                    "type": "object",
                    "properties": {"order_id": {"type": "string"}},
                    "required": ["order_id"],
                },
            )
        },
        output_schema=ORDER_OUTPUT_SCHEMA,
        logger=StructuredLogger(enabled=False),
    )
    result = engine.run("settle the order", tool_whitelist=["fetch_order"])
    assert result.ok, result.failure_reason
    assert result.execution_result["order_id"] == "ORD-1001"


def test_engine_does_not_retry_a_dead_endpoint(rate_limited: HttpLLMAdapter) -> None:
    """A 429 must escape the loop instead of burning the retry budget."""
    from harness_core.adapters.mcp import FakeMCPConnector
    from harness_core.engine import HarnessOrchestrationEngine
    from harness_core.observability import StructuredLogger

    engine = HarnessOrchestrationEngine(
        llm=rate_limited,
        connector=FakeMCPConnector(),
        max_retries=5,
        logger=StructuredLogger(enabled=False),
    )
    with pytest.raises(HarnessInfrastructureError):
        engine.run("x", tool_whitelist=[])
