"""Streaming SSE: return on the reply, not on the connection closing.

The behaviour under test
------------------------
An MCP server is allowed to answer with ``text/event-stream`` and keep the stream
open afterwards (keep-alives, server-initiated notifications). A client that calls
``response.read()`` waits for EOF — so it waits for the server to close, which may
be never. That turns a 300 ms reply into a timeout, and a timeout into a spurious
"server is broken".

These tests prove the client returns as soon as the matching message arrives. The
assertion is on **elapsed time**, because that is the only observable difference
between streaming and read-to-EOF: both produce the same reply body.

The stub's ``--hold N`` flag keeps the connection open for N seconds after the
reply. With N=5 and a client timeout of 10, a read-to-EOF client takes ~5 s; a
streaming client takes well under 1 s. Asserting "< 3 s" therefore fails on the
non-streaming implementation and passes on the streaming one.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Iterator

import pytest

from harness_core.adapters.mcp_http import (
    HttpMCPConnector,
    MCPHttpError,
    _read_sse_until,
)

STUB = str(Path(__file__).parent / "mcp_http_server_stub.py")

#: How long the stub keeps the stream open after replying. Comfortably longer than
#: the assertion threshold, comfortably shorter than the client timeout.
HOLD_SECONDS = 5.0
#: The reply must arrive well before the hold expires.
FAST_ENOUGH = 3.0


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

    def state(self) -> dict:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/state", timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)


@pytest.fixture
def holding_server() -> Iterator[StubServer]:
    """An SSE server that replies and then keeps the stream open."""
    server = StubServer("--sse", "--hold", str(HOLD_SECONDS))
    try:
        yield server
    finally:
        server.close()


# ---------------------------------------------------------------------------
# the timing proof
# ---------------------------------------------------------------------------


def test_a_held_open_stream_returns_before_it_closes(holding_server: StubServer) -> None:
    """The whole point of streaming: the reply arrives while the stream is open."""
    conn = HttpMCPConnector(holding_server.url, timeout=HOLD_SECONDS * 2)
    try:
        started = time.perf_counter()
        conn.initialize()
        elapsed = time.perf_counter() - started
    finally:
        conn.close()

    assert elapsed < FAST_ENOUGH, (
        f"initialize took {elapsed:.2f}s with a {HOLD_SECONDS}s hold — the client "
        "waited for the stream to close instead of returning on the reply"
    )


def test_a_held_open_stream_returns_a_correct_handshake(holding_server: StubServer) -> None:
    """Speed is worthless if the payload is wrong."""
    conn = HttpMCPConnector(holding_server.url, timeout=HOLD_SECONDS * 2)
    try:
        info = conn.initialize()
        assert info["protocolVersion"]
        assert conn.session_id, "the session id must still be captured from the header"
    finally:
        conn.close()


def test_tool_listing_works_over_a_held_stream(holding_server: StubServer) -> None:
    conn = HttpMCPConnector(holding_server.url, timeout=HOLD_SECONDS * 2)
    try:
        conn.initialize()
        tools = conn.list_tools()
        assert {t.name for t in tools} >= {"echo", "add", "fail"}
    finally:
        conn.close()


def test_tool_calls_work_over_a_held_stream(holding_server: StubServer) -> None:
    conn = HttpMCPConnector(holding_server.url, timeout=HOLD_SECONDS * 2)
    try:
        conn.initialize()
        result = conn.call_tool("add", {"a": 2, "b": 3})
        assert result.ok
        assert "5" in str(result.content)
    finally:
        conn.close()


def test_several_requests_each_return_promptly(holding_server: StubServer) -> None:
    """The hold must not accumulate: each request returns on its own reply."""
    conn = HttpMCPConnector(holding_server.url, timeout=HOLD_SECONDS * 2)
    try:
        conn.initialize()
        started = time.perf_counter()
        conn.list_tools()
        conn.call_tool("echo", {"text": "hi"})
        elapsed = time.perf_counter() - started
    finally:
        conn.close()
    assert elapsed < FAST_ENOUGH * 2, f"two requests took {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# the parser, in isolation
# ---------------------------------------------------------------------------


class FakeStream:
    """A minimal stand-in for an HTTP response: iterable of byte lines."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)
        self.consumed = 0

    def __iter__(self) -> Iterator[bytes]:
        for line in self._lines:
            self.consumed += 1
            yield line


def test_read_sse_stops_at_the_matching_id() -> None:
    """It must not consume the rest of the stream after finding the reply."""
    stream = FakeStream(
        [
            b": keepalive\n",
            b"\n",
            b'data: {"jsonrpc":"2.0","id":7,"result":{"ok":true}}\n',
            b"\n",
            b'data: {"jsonrpc":"2.0","id":8,"result":{"other":true}}\n',
            b"\n",
        ]
    )
    body, matched = _read_sse_until(stream, 7)
    assert matched is not None
    assert matched["id"] == 7
    assert stream.consumed == 3, "reading continued past the reply"
    assert "keepalive" in body


def test_read_sse_skips_other_ids() -> None:
    stream = FakeStream(
        [
            b'data: {"jsonrpc":"2.0","id":1,"result":{}}\n',
            b'data: {"jsonrpc":"2.0","id":2,"result":{"wanted":true}}\n',
        ]
    )
    _, matched = _read_sse_until(stream, 2)
    assert matched is not None
    assert matched["result"] == {"wanted": True}


def test_read_sse_returns_none_when_the_id_never_arrives() -> None:
    stream = FakeStream([b'data: {"jsonrpc":"2.0","id":1,"result":{}}\n'])
    _, matched = _read_sse_until(stream, 99)
    assert matched is None


def test_read_sse_skips_malformed_chunks() -> None:
    stream = FakeStream(
        [
            b"data: not json\n",
            b'data: {"jsonrpc":"2.0","id":3,"result":{"ok":true}}\n',
        ]
    )
    _, matched = _read_sse_until(stream, 3)
    assert matched is not None
    assert matched["id"] == 3


def test_read_sse_skips_done_and_comments() -> None:
    stream = FakeStream(
        [
            b": comment\n",
            b"data: [DONE]\n",
            b'data: {"jsonrpc":"2.0","id":4,"result":{}}\n',
        ]
    )
    _, matched = _read_sse_until(stream, 4)
    assert matched is not None


# ---------------------------------------------------------------------------
# plain JSON is still handled
# ---------------------------------------------------------------------------


def test_a_plain_json_server_still_works() -> None:
    """Streaming must not regress the non-streaming path."""
    server = StubServer()
    conn = HttpMCPConnector(server.url, timeout=10)
    try:
        conn.initialize()
        assert conn.call_tool("add", {"a": 1, "b": 2}).ok
    finally:
        conn.close()
        server.close()


def test_an_sse_server_without_hold_still_works() -> None:
    server = StubServer("--sse")
    conn = HttpMCPConnector(server.url, timeout=10)
    try:
        conn.initialize()
        assert conn.call_tool("echo", {"text": "x"}).ok
    finally:
        conn.close()
        server.close()


# ---------------------------------------------------------------------------
# errors are still classified correctly while streaming
# ---------------------------------------------------------------------------


def test_a_jsonrpc_error_over_a_held_stream_still_raises() -> None:
    server = StubServer("--sse", "--rpc-error", "--hold", "2")
    conn = HttpMCPConnector(server.url, timeout=10)
    try:
        with pytest.raises(MCPHttpError, match="error"):
            conn.initialize()
    finally:
        conn.close()
        server.close()


def test_the_server_reports_that_it_held_the_connection(holding_server: StubServer) -> None:
    """Confirms the fixture actually holds: without this the timing test is vacuous.

    If ``--hold`` silently did nothing, every timing assertion above would pass on a
    non-streaming client too — the test would be measuring nothing.
    """
    conn = HttpMCPConnector(holding_server.url, timeout=HOLD_SECONDS * 2)
    try:
        conn.initialize()
    finally:
        conn.close()
    # The handler increments `held` before sleeping; it may still be sleeping, so
    # poll briefly rather than assuming instant visibility.
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if holding_server.state().get("held", 0) > 0:
            break
        time.sleep(0.05)
    assert holding_server.state()["held"] > 0, "--hold did not take effect"
