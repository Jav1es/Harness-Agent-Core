"""HTTP/SSE MCP transport, tested against a real localhost server.

Every test spawns ``tests/mcp_http_server_stub.py`` and speaks real HTTP to it. No
network, no SDK — but also no mocked transport, so the handshake, session tracking,
body parsing and error classification are all exercised for real.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import pytest

from harness_core.adapters.mcp import MCPConnector
from harness_core.adapters.mcp_http import (
    HttpMCPConnector,
    MCPHttpError,
    MCPHttpTimeoutError,
    _parse_messages,
)
from harness_core.errors import HarnessInfrastructureError

STUB = str(Path(__file__).parent / "mcp_http_server_stub.py")


class StubServer:
    def __init__(self, *flags: str) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, STUB, *flags],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.port = self._wait()

    def _wait(self, timeout: float = 15.0) -> int:
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
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)


def _serve(*flags: str) -> Iterator[HttpMCPConnector]:
    server = StubServer(*flags)
    conn = HttpMCPConnector(server.url, timeout=10)
    try:
        yield conn
    finally:
        conn.close()
        server.close()


@pytest.fixture
def conn() -> Iterator[HttpMCPConnector]:
    yield from _serve()


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_lists_tools(conn: HttpMCPConnector) -> None:
    assert [t.name for t in conn.list_tools()] == ["echo", "add", "fail"]


def test_tool_schema_survives_the_wire(conn: HttpMCPConnector) -> None:
    add = next(t for t in conn.list_tools() if t.name == "add")
    assert add.input_schema["required"] == ["a", "b"]


def test_calls_a_tool(conn: HttpMCPConnector) -> None:
    result = conn.call_tool("echo", {"text": "hello"})
    assert result.ok
    assert result.content == "hello"
    assert result.meta["transport"] == "http"


def test_arithmetic_round_trips(conn: HttpMCPConnector) -> None:
    assert conn.call_tool("add", {"a": 2, "b": 3}).content == "5.0"


def test_a_tool_level_failure_is_a_normal_outcome(conn: HttpMCPConnector) -> None:
    result = conn.call_tool("fail", {})
    assert result.ok is False
    assert result.error == "this tool always fails"


def test_satisfies_the_connector_protocol(conn: HttpMCPConnector) -> None:
    assert isinstance(conn, MCPConnector)


# ---------------------------------------------------------------------------
# session handling
# ---------------------------------------------------------------------------


def test_a_session_id_is_captured(conn: HttpMCPConnector) -> None:
    conn.initialize()
    assert conn.session_id


def _server_state(server: StubServer) -> dict:
    """Ask the stub what it actually received."""
    import json as _json
    import urllib.request as _request

    with _request.urlopen(f"http://127.0.0.1:{server.port}/state", timeout=5) as response:
        return _json.loads(response.read().decode("utf-8"))


def test_the_session_id_is_echoed_on_later_requests() -> None:
    """The server must actually *see* the session id on later requests.

    Asserting only ``conn.session_id is not None`` proves the client stored it, not
    that it sent it — and sending it is the part that breaks. This reads the server's
    side of the conversation, across the process boundary.
    """
    server = StubServer()
    conn = HttpMCPConnector(server.url, timeout=10)
    try:
        conn.initialize()
        session = conn.session_id
        assert session
        conn.list_tools()
        time.sleep(0.2)
        state = _server_state(server)
        # initialize is the first POST and carries no session; the rest must.
        assert state["session_headers"][0] is None
        assert session in state["session_headers"][1:], state["session_headers"]
    finally:
        conn.close()
        server.close()


def test_close_releases_the_session_on_the_server() -> None:
    """A leaked session keeps server state alive until its own timeout."""
    server = StubServer()
    conn = HttpMCPConnector(server.url, timeout=10)
    try:
        conn.initialize()
        session = conn.session_id
        assert session
        conn.close()
        time.sleep(0.2)
        state = _server_state(server)
        assert session in state["deleted"]
        assert session not in state["live_sessions"]
    finally:
        server.close()


def test_close_sends_delete_for_the_session() -> None:
    """A server never told the session ended leaks state until its own timeout."""
    server = StubServer()
    conn = HttpMCPConnector(server.url, timeout=10)
    try:
        conn.initialize()
        session = conn.session_id
        assert session
        conn.close()
        time.sleep(0.2)
        state = _server_state(server)
        assert session in state["deleted"]
    finally:
        server.close()


def test_close_is_idempotent() -> None:
    server = StubServer()
    conn = HttpMCPConnector(server.url, timeout=10)
    try:
        conn.initialize()
        conn.close()
        conn.close()
    finally:
        server.close()


def test_works_without_a_session_id() -> None:
    """Not every server issues one; its absence must not break the client."""
    gen = _serve("--no-session")
    conn = next(gen)
    try:
        assert [t.name for t in conn.list_tools()] == ["echo", "add", "fail"]
        assert conn.session_id is None
        assert conn.call_tool("echo", {"text": "x"}).content == "x"
    finally:
        gen.close()


# ---------------------------------------------------------------------------
# SSE bodies
# ---------------------------------------------------------------------------


def test_sse_responses_are_parsed() -> None:
    gen = _serve("--sse")
    conn = next(gen)
    try:
        assert [t.name for t in conn.list_tools()] == ["echo", "add", "fail"]
        assert conn.call_tool("echo", {"text": "via sse"}).content == "via sse"
    finally:
        gen.close()


def test_sse_parser_skips_keepalives_and_events() -> None:
    """Keepalives must be skipped *without* losing the real message.

    Counting messages alone is not enough: a parser that strips nothing would also
    return one message here if it happened to be lenient, and a parser that loses
    everything returns zero. The assertion is on the message *identity*, so both
    mistakes fail.
    """
    body = (
        ": keepalive\n\n"
        "event: message\n"
        'data: {"jsonrpc": "2.0", "id": 1, "result": {"ok": true}}\n\n'
    )
    messages = _parse_messages(body, "text/event-stream")
    assert len(messages) == 1
    assert messages[0]["id"] == 1
    assert messages[0]["result"] == {"ok": True}


def test_sse_parser_strips_the_data_prefix() -> None:
    """The `data: ` prefix must be removed before parsing, not merely tolerated.

    Without the strip, the line ``data: {"id": 1}`` is not valid JSON and the whole
    message is lost — which looks exactly like "the server sent nothing".
    """
    body = 'data: {"jsonrpc": "2.0", "id": 7, "result": {"n": 1}}\n\n'
    messages = _parse_messages(body, "text/event-stream")
    assert len(messages) == 1
    assert messages[0]["id"] == 7


def test_sse_parser_handles_multiple_messages() -> None:
    body = 'data: {"id": 1}\n\ndata: {"id": 2}\n\n'
    messages = _parse_messages(body, "text/event-stream")
    assert [m["id"] for m in messages] == [1, 2]


def test_sse_parser_ignores_done_marker() -> None:
    body = 'data: {"id": 1}\n\ndata: [DONE]\n\n'
    assert len(_parse_messages(body, "text/event-stream")) == 1


def test_sse_parser_skips_a_malformed_chunk() -> None:
    body = 'data: {broken\ndata: {"id": 1}\n\n'
    messages = _parse_messages(body, "text/event-stream")
    assert len(messages) == 1


def test_json_parser_accepts_a_batch() -> None:
    body = '[{"id": 1}, {"id": 2}]'
    assert len(_parse_messages(body, "application/json")) == 2


# ---------------------------------------------------------------------------
# transport failures escape the loop
# ---------------------------------------------------------------------------


def test_http_error_raises() -> None:
    gen = _serve("--status", "500")
    conn = next(gen)
    try:
        with pytest.raises(MCPHttpError, match="HTTP 500"):
            conn.list_tools()
    finally:
        gen.close()


def test_a_transport_error_is_an_infrastructure_error() -> None:
    gen = _serve("--status", "503")
    conn = next(gen)
    try:
        with pytest.raises(HarnessInfrastructureError):
            conn.list_tools()
    finally:
        gen.close()


def test_a_non_json_body_raises() -> None:
    gen = _serve("--garbage")
    conn = next(gen)
    try:
        with pytest.raises(MCPHttpError, match="non-JSON"):
            conn.list_tools()
    finally:
        gen.close()


def test_an_unreachable_endpoint_raises() -> None:
    conn = HttpMCPConnector("http://127.0.0.1:1/mcp", timeout=2)
    with pytest.raises(MCPHttpError):
        conn.list_tools()


def test_a_timeout_raises() -> None:
    gen = _serve("--slow", "3")
    conn = next(gen)
    conn.timeout = 0.5
    try:
        with pytest.raises(MCPHttpError):
            conn.list_tools()
    finally:
        gen.close()


def test_an_empty_reply_is_a_transport_error() -> None:
    """A 200 with no matching message is not a valid answer."""
    gen = _serve("--silent")
    conn = next(gen)
    try:
        with pytest.raises(MCPHttpError):
            conn.list_tools()
    finally:
        gen.close()


def test_a_jsonrpc_error_raises() -> None:
    gen = _serve("--rpc-error")
    conn = next(gen)
    try:
        with pytest.raises(MCPHttpError, match="returned an error"):
            conn.list_tools()
    finally:
        gen.close()


# ---------------------------------------------------------------------------
# headers and configuration
# ---------------------------------------------------------------------------


def test_auth_headers_are_sent() -> None:
    """The server must actually receive the Authorization header."""
    server = StubServer()
    conn = HttpMCPConnector(
        server.url, headers={"Authorization": "Bearer token-123"}, timeout=10
    )
    try:
        conn.initialize()
        time.sleep(0.2)
        state = _server_state(server)
        assert state["auth_headers"], "no request reached the server"
        assert all(
            header == "Bearer token-123" for header in state["auth_headers"]
        ), state["auth_headers"]
    finally:
        conn.close()
        server.close()


def test_no_auth_header_is_sent_when_none_is_configured() -> None:
    """A connector must not invent credentials."""
    server = StubServer()
    conn = HttpMCPConnector(server.url, timeout=10)
    try:
        conn.initialize()
        time.sleep(0.2)
        state = _server_state(server)
        assert all(header is None for header in state["auth_headers"]), state["auth_headers"]
    finally:
        conn.close()
        server.close()


def test_initialize_is_idempotent(conn: HttpMCPConnector) -> None:
    first = conn.initialize()
    second = conn.initialize()
    assert second["protocolVersion"] == first["protocolVersion"]


def test_the_timeout_is_configurable() -> None:
    conn = HttpMCPConnector("http://127.0.0.1:1/mcp", timeout=1.5)
    assert conn.timeout == 1.5


def test_timeout_error_is_a_subclass() -> None:
    assert issubclass(MCPHttpTimeoutError, MCPHttpError)


# ---------------------------------------------------------------------------
# engine integration
# ---------------------------------------------------------------------------


def test_engine_runs_against_a_real_http_mcp_server() -> None:
    from harness_core.adapters.llm import FakeLLMAdapter, LLMResponse, ToolCallRequest
    from harness_core.engine import HarnessOrchestrationEngine
    from harness_core.observability import StructuredLogger

    server = StubServer()
    connector = HttpMCPConnector(server.url, timeout=10)
    llm = FakeLLMAdapter(
        [
            LLMResponse(tool_call=ToolCallRequest("echo", {"text": "hi"})),
            LLMResponse(content={"done": True}),
        ]
    )
    engine = HarnessOrchestrationEngine(
        llm=llm,
        connector=connector,
        logger=StructuredLogger(enabled=False),
    )
    try:
        result = engine.run("say hi", tool_whitelist=["echo"])
    finally:
        connector.close()
        server.close()
    assert result.ok, result.failure_reason
    assert result.execution_result == {"done": True}


def test_engine_does_not_retry_a_dead_http_endpoint() -> None:
    """A dead transport must escape, not burn the retry budget."""
    from harness_core.adapters.llm import FakeLLMAdapter
    from harness_core.engine import HarnessOrchestrationEngine
    from harness_core.observability import StructuredLogger

    engine = HarnessOrchestrationEngine(
        llm=FakeLLMAdapter([]),
        connector=HttpMCPConnector("http://127.0.0.1:1/mcp", timeout=2),
        max_retries=5,
        logger=StructuredLogger(enabled=False),
    )
    with pytest.raises(HarnessInfrastructureError):
        engine.run("x", tool_whitelist=[])
