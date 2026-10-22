"""A minimal OpenAI-compatible chat-completions server, used by tests.

Real HTTP over a real socket on localhost, so ``HttpLLMAdapter`` is exercised end to
end: request shape, auth header, JSON parsing, error classification and timeouts.

Failure modes are selected by argv so the tests can drive paths a healthy endpoint
would never produce:

    python llm_server_stub.py                 # normal
    python llm_server_stub.py --status 429    # always that HTTP status
    python llm_server_stub.py --no-choices    # 200 with an unusable body
    python llm_server_stub.py --empty         # 200 with empty content
    python llm_server_stub.py --not-json      # 200 with a non-JSON body
    python llm_server_stub.py --slow 3        # delay the response
    python llm_server_stub.py --echo          # echo the request back as content

The server prints ``PORT <n>`` on stdout once listening, so the test can connect
without racing on a fixed port.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def _message(content: Any, tool_calls: list[dict] | None = None) -> dict:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _wants_a_tool(prompt: str) -> bool:
    """The stub's stand-in for intent detection.

    Tests that want a tool call say so explicitly (``"use a tool"`` or
    ``"fetch the order"``); everything else gets a deliverable. Without this the
    stub would call a tool on every turn and no run could ever terminate.
    """
    lowered = prompt.lower()
    return "use a tool" in lowered or "fetch" in lowered


def _tool_call(name: str, arguments: str) -> dict:
    return {
        "id": "call_1",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


class StubState:
    def __init__(self, argv: list[str]) -> None:
        self.status = 200
        if "--status" in argv:
            self.status = int(argv[argv.index("--status") + 1])
        self.no_choices = "--no-choices" in argv
        self.empty = "--empty" in argv
        self.not_json = "--not-json" in argv
        self.echo = "--echo" in argv
        self.slow = float(argv[argv.index("--slow") + 1]) if "--slow" in argv else 0.0
        #: Every request body received, for assertions on what the adapter sent.
        self.requests: list[dict[str, Any]] = []
        self.auth_headers: list[str | None] = []
        #: How many chat-completions calls have been served (drives tool-then-deliver).
        self.turns = 0


class Handler(BaseHTTPRequestHandler):
    state: StubState

    def log_message(self, *args: Any) -> None:  # silence the default stderr spam
        return None

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            body = {"__unparsed__": raw.decode("utf-8", "replace")}
        self.state.requests.append(body)
        self.state.auth_headers.append(self.headers.get("Authorization"))

        if self.state.slow:
            time.sleep(self.state.slow)

        if self.state.status != 200:
            self._send_json(self.state.status, {"error": {"message": "stubbed failure"}})
            return

        if self.state.not_json:
            payload = b"this is not json"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if self.state.no_choices:
            self._send_json(200, {"id": "x", "model": "stub", "choices": []})
            return

        if self.state.empty:
            self._send_json(
                200,
                {
                    "id": "x",
                    "model": "stub",
                    "choices": [{"index": 0, "message": _message("")}],
                    "usage": {"total_tokens": 5},
                },
            )
            return

        if self.state.echo:
            content = json.dumps({"echo": body}, ensure_ascii=False)
            self._send_json(
                200,
                {
                    "id": "x",
                    "model": "stub",
                    "choices": [{"index": 0, "message": _message(content)}],
                    "usage": {"total_tokens": 11},
                },
            )
            return

        # Default behaviour: call a tool on the *first* turn when the prompt asks for
        # one, then deliver. The turn counter matters: if the stub called a tool on
        # every turn, no run could ever terminate and every engine test would spin
        # to max_steps. A real model stops calling tools once it has the data.
        messages = body.get("messages") or []
        tools = body.get("tools") or []
        prompt = str(messages[0].get("content", "")) if messages else ""
        if tools and self.state.turns == 0 and _wants_a_tool(prompt):
            self.state.turns += 1
            name = tools[0]["function"]["name"]
            self._send_json(
                200,
                {
                    "id": "x",
                    "model": "stub",
                    "choices": [
                        {
                            "index": 0,
                            "message": _message(
                                None, [_tool_call(name, json.dumps({"order_id": "ORD-1001"}))]
                            ),
                        }
                    ],
                    "usage": {"total_tokens": 42},
                },
            )
            return
        self.state.turns += 1

        deliverable = {
            "order_id": "ORD-1001",
            "currency": "CNY",
            "amount": 1200.0,
            "status": "settled",
        }
        self._send_json(
            200,
            {
                "id": "x",
                "model": "stub",
                "choices": [{"index": 0, "message": _message(json.dumps(deliverable))}],
                "usage": {"total_tokens": 42},
            },
        )


def main(argv: list[str]) -> int:
    state = StubState(argv)
    handler = type("BoundHandler", (Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    print(f"PORT {port}", flush=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        thread.join()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
