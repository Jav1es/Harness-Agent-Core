"""A minimal but real MCP server, used by the tests as a subprocess.

This speaks newline-delimited JSON-RPC 2.0 over stdio — the actual MCP transport —
so ``StdioMCPConnector`` is exercised end to end rather than against a mock. It
implements just enough of the protocol for a client to connect, list tools and call
one.

Deliberately broken behaviours are selected by argv so the tests can drive failure
paths that a well-behaved server would never produce:

    python mcp_server_stub.py              # normal
    python mcp_server_stub.py --crash      # exit before answering initialize
    python mcp_server_stub.py --hang       # accept requests, never answer
    python mcp_server_stub.py --garbage    # print non-JSON noise on stdout
    python mcp_server_stub.py --slow 0.5   # delay every response
"""

from __future__ import annotations

import json
import sys
import time

PROTOCOL_VERSION = "2024-11-05"


def _send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _result(request_id: object, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id: object, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def _text_result(text: str, *, is_error: bool = False) -> dict:
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }


def main(argv: list[str]) -> int:
    crash = "--crash" in argv
    hang = "--hang" in argv
    garbage = "--garbage" in argv
    slow = 0.0
    if "--slow" in argv:
        slow = float(argv[argv.index("--slow") + 1])

    if crash:
        sys.stderr.write("stub: refusing to start\n")
        return 1

    if garbage:
        # Noise on stdout: a client must skip it rather than choke.
        sys.stdout.write("starting up...\n")
        sys.stdout.flush()

    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            continue

        if hang:
            # Accept the request and answer nothing: exercises the timeout path.
            continue

        if slow:
            time.sleep(slow)

        method = message.get("method")
        request_id = message.get("id")

        if method == "initialize":
            _result(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "stub-server", "version": "0.0.1"},
                },
            )
        elif method == "notifications/initialized":
            continue  # notification: no response
        elif method == "tools/list":
            _result(
                request_id,
                {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Echo the given text back",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"],
                            },
                        },
                        {
                            "name": "add",
                            "description": "Add two integers",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "a": {"type": "integer"},
                                    "b": {"type": "integer"},
                                },
                                "required": ["a", "b"],
                            },
                        },
                        {
                            "name": "fail",
                            "description": "Always reports a tool-level error",
                            "inputSchema": {"type": "object"},
                        },
                        {
                            "name": "boom",
                            "description": "Returns a JSON-RPC error instead of a result",
                            "inputSchema": {"type": "object"},
                        },
                    ]
                },
            )
        elif method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "echo":
                _result(request_id, _text_result(str(args.get("text", ""))))
            elif name == "add":
                try:
                    total = int(args.get("a", 0)) + int(args.get("b", 0))
                except (TypeError, ValueError):
                    _result(request_id, _text_result("a and b must be integers", is_error=True))
                else:
                    _result(request_id, _text_result(str(total)))
            elif name == "fail":
                # A tool-level failure: isError=True, still a valid JSON-RPC result.
                _result(request_id, _text_result("this tool always fails", is_error=True))
            elif name == "boom":
                # A protocol-level error: the client must raise, not return ok=False.
                _error(request_id, -32603, "internal error from boom")
            else:
                _error(request_id, -32602, f"unknown tool {name!r}")
        elif request_id is not None:
            _error(request_id, -32601, f"unknown method {method!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
