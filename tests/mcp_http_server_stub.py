"""A minimal MCP-over-HTTP server, used by tests.

Real HTTP on localhost with a real session id, so ``HttpMCPConnector`` is exercised
end to end: the handshake, session tracking, JSON bodies, SSE bodies, and the
error classification.

Failure modes are selected by argv:

    python mcp_http_server_stub.py              # JSON responses
    python mcp_http_server_stub.py --sse        # SSE responses
    python mcp_http_server_stub.py --status 500 # always that HTTP status
    python mcp_http_server_stub.py --no-session # never hand out a session id
    python mcp_http_server_stub.py --garbage    # 200 with a non-JSON body
    python mcp_http_server_stub.py --slow 3     # delay the response
    python mcp_http_server_stub.py --silent     # accept, never answer the id
    python mcp_http_server_stub.py --rpc-error  # every request is a JSON-RPC error

Prints ``PORT <n>`` on stdout once listening.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "echo",
        "description": "Echo the given text",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
    {
        "name": "fail",
        "description": "Always fails at the tool level",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class StubState:
    def __init__(self, argv: list[str]) -> None:
        self.status = int(argv[argv.index("--status") + 1]) if "--status" in argv else 200
        self.sse = "--sse" in argv
        self.no_session = "--no-session" in argv
        self.garbage = "--garbage" in argv
        self.slow = float(argv[argv.index("--slow") + 1]) if "--slow" in argv else 0.0
        self.silent = "--silent" in argv
        self.rpc_error = "--rpc-error" in argv
        #: ``--hold N`` keeps an SSE response open for N seconds after the reply.
        #: This is how the tests prove the client returns on the *message* rather
        #: than on EOF: a read-to-EOF client blocks for N seconds, a streaming one
        #: returns immediately.
        self.hold = float(argv[argv.index("--hold") + 1]) if "--hold" in argv else 0.0
        self.held = 0
        self.sessions: dict[str, int] = {}
        self.deleted: list[str] = []
        self.requests: list[dict[str, Any]] = []
        self.session_headers: list[str | None] = []
        self.auth_headers: list[str | None] = []
        self.methods: list[str] = []
        #: The session id most recently handed out, echoed back as a response header.
        self.response_session: str | None = None


def _result_for(method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "stub-http", "version": "1.0"},
        }
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "echo":
            return {"content": [{"type": "text", "text": str(args.get("text", ""))}]}
        if name == "add":
            total = float(args.get("a", 0)) + float(args.get("b", 0))
            return {"content": [{"type": "text", "text": str(total)}]}
        if name == "fail":
            return {
                "content": [{"type": "text", "text": "this tool always fails"}],
                "isError": True,
            }
        return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}
    return {}


class Handler(BaseHTTPRequestHandler):
    state: StubState

    def log_message(self, *args: Any) -> None:
        return None

    # ---------------------------------------------------------------- helpers
    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        session = self.state.response_session
        if session and not self.state.no_session:
            self.send_header("Mcp-Session-Id", session)
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, payload: dict[str, Any]) -> None:
        keepalive = b": keepalive\n\n"
        body = ("data: " + json.dumps(payload) + "\n\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        if not self.state.hold:
            # The keepalive is part of the body, so it must be part of the length.
            # Omitting it truncates the response mid-JSON, which looks exactly like a
            # broken server rather than a broken stub.
            self.send_header("Content-Length", str(len(keepalive) + len(body)))
        session = self.state.response_session
        if session and not self.state.no_session:
            self.send_header("Mcp-Session-Id", session)
        self.end_headers()
        self.wfile.write(keepalive)
        self.wfile.flush()
        self.wfile.write(body)
        self.wfile.flush()
        if self.state.hold:
            # Never close. A client that reads to EOF blocks here until its timeout;
            # a streaming client returns as soon as it sees the reply.
            self.state.held += 1
            try:
                time.sleep(self.state.hold)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _reply(self, payload: dict[str, Any]) -> None:
        if self.state.sse:
            self._send_sse(payload)
        else:
            self._send_json(200, payload)

    # ----------------------------------------------------------------- verbs
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        """Expose what the server actually received.

        The tests need the *server's* view — "did the client really send the session
        id?" cannot be answered by asking the client. A test-only endpoint is the
        only way to observe it across the process boundary.
        """
        payload = {
            "methods": list(self.state.methods),
            "session_headers": list(self.state.session_headers),
            "auth_headers": list(self.state.auth_headers),
            "deleted": list(self.state.deleted),
            "live_sessions": list(self.state.sessions),
            "request_count": len(self.state.requests),
            "held": self.state.held,
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self) -> None:  # noqa: N802 - http.server API
        session = self.headers.get("Mcp-Session-Id")
        self.state.session_headers.append(session)
        if session:
            self.state.deleted.append(session)
            self.state.sessions.pop(session, None)
        self.send_response(204)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        message = self._body()
        method = str(message.get("method", ""))
        self.state.requests.append(message)
        self.state.methods.append(method)
        self.state.session_headers.append(self.headers.get("Mcp-Session-Id"))
        self.state.auth_headers.append(self.headers.get("Authorization"))

        if self.state.slow:
            time.sleep(self.state.slow)

        if self.state.status != 200:
            self._send_json(self.state.status, {"error": {"message": "stubbed failure"}})
            return

        if self.state.garbage:
            payload = b"not json at all"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        # Notifications have no id and get no reply body.
        if "id" not in message:
            self.send_response(202)
            self.end_headers()
            return

        if self.state.silent:
            # Accept the request, answer nothing: the client must time out.
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if self.state.rpc_error:
            self._reply(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": "method not found (stubbed)"},
                }
            )
            return

        if method == "initialize" and not self.state.no_session:
            # Hand out a session id. Note: the *received* header was recorded above,
            # before this line, so the record reflects what the client sent — not
            # what we are about to assign.
            new_session = uuid.uuid4().hex
            self.state.sessions[new_session] = 1
            self.state.response_session = new_session

        self._reply(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": _result_for(method, message.get("params") or {}),
            }
        )


def main(argv: list[str]) -> int:
    state = StubState(argv)
    handler = type("BoundHandler", (Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    print(f"PORT {server.server_address[1]}", flush=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        thread.join()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
