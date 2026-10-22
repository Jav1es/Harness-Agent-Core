"""A real MCP stdio client, implemented on the standard library only.

Why not use the ``mcp`` SDK
--------------------------
MCP over stdio is *newline-delimited JSON-RPC 2.0* on a child process's
stdin/stdout. That is a small, stable protocol, so implementing it directly buys
three things the SDK cannot:

* **Zero runtime dependencies.** The package's whole premise is that the harness
  layer runs anywhere, offline, with no vendor binding.
* **Testability.** A test can spawn a real subprocess that speaks the protocol, so
  the connector is exercised end-to-end instead of being a documented skeleton.
* **No version coupling.** The SDK's API surface moves; the wire format does not.

What this file does *not* do: HTTP/SSE transports, server-side features, or
sampling. It is a client for local tool servers, which is what a harness needs.

Failure policy
--------------
Every failure mode is classified, because they must not be treated alike:

* **Server unavailable / crashed / timed out** → ``HarnessInfrastructureError``.
  Retrying cannot fix a missing binary, and dressing it up as a tool failure would
  ask the model to "repair" an environment problem.
* **Server returned a JSON-RPC error or ``isError: true``** → ``ToolResult(ok=False)``.
  That is a normal business outcome the self-healing loop should see.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Sequence

from ..errors import HarnessInfrastructureError
from .mcp import ToolDefinition, ToolResult

#: Protocol revision this client speaks. MCP is date-versioned.
PROTOCOL_VERSION = "2024-11-05"

#: How long to wait for one response before declaring the server unresponsive.
DEFAULT_TIMEOUT = 10.0


class MCPTransportError(HarnessInfrastructureError):
    """The server could not be reached, spoke nonsense, or died mid-call."""


class MCPTimeoutError(MCPTransportError):
    """The server accepted the request but never answered."""


@dataclass
class _Pending:
    event: threading.Event
    response: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class StdioMCPConnector:
    """Spawns an MCP server and talks JSON-RPC 2.0 to it over stdio.

    Usage::

        with StdioMCPConnector("python", ["server.py"]) as conn:
            tools = conn.list_tools()
            result = conn.call_tool("add", {"a": 1, "b": 2})

    The connector is synchronous by contract (the harness is synchronous); a
    background reader thread turns the blocking pipe into a queue so timeouts work
    on Windows, where ``select`` cannot poll pipes.
    """

    name = "stdio-mcp"

    def __init__(
        self,
        command: str,
        args: Sequence[str] | None = None,
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        client_name: str = "harness-agent-core",
        client_version: str = "0.1.0",
    ) -> None:
        self.command = command
        self.args = list(args or [])
        self.env = env
        self.cwd = cwd
        self.timeout = timeout
        self.client_name = client_name
        self.client_version = client_version

        self._proc: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._pending: dict[int, _Pending] = {}
        self._lock = threading.Lock()
        self._next_id = 0
        self._tools: list[ToolDefinition] | None = None
        self._stderr_tail: list[str] = []
        self._closed = False

    # ------------------------------------------------------------------ setup
    def _spawn(self) -> None:
        if self._proc is not None:
            return
        environ = dict(os.environ)
        if self.env:
            environ.update(self.env)
        try:
            self._proc = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,  # line-buffered: JSON-RPC over stdio is line-delimited
                env=environ,
                cwd=self.cwd,
            )
        except (OSError, ValueError) as exc:
            raise MCPTransportError(
                f"could not start MCP server {self.command!r}: {exc}"
            ) from exc

        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:  # pragma: no cover - defensive
            self._lines.put(None)
            return
        for line in proc.stdout:
            self._lines.put(line)
        # EOF: the server closed stdout. Wake anyone waiting.
        self._lines.put(None)
        with self._lock:
            for pending in self._pending.values():
                pending.event.set()

    def _read_stderr(self) -> None:
        """Drain stderr so a chatty server cannot deadlock on a full pipe."""
        proc = self._proc
        if proc is None or proc.stderr is None:  # pragma: no cover - defensive
            return
        for line in proc.stderr:
            text = line.rstrip()
            if text:
                self._stderr_tail.append(text)
                del self._stderr_tail[:-20]

    # -------------------------------------------------------------- transport
    def _send(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise MCPTransportError("MCP server is not running")
        payload = json.dumps(message, ensure_ascii=False)
        try:
            proc.stdin.write(payload + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise MCPTransportError(
                f"MCP server closed its stdin: {exc}{self._stderr_hint()}"
            ) from exc

    def _stderr_hint(self) -> str:
        if not self._stderr_tail:
            return ""
        return " | server stderr: " + " / ".join(self._stderr_tail[-3:])

    def _next_request_id(self) -> int:
        with self._lock:
            self._next_id += 1
            return self._next_id

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a request and wait for its response, honouring the timeout."""
        self._spawn()
        request_id = self._next_request_id()
        pending = _Pending(event=threading.Event())
        with self._lock:
            self._pending[request_id] = pending

        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

        deadline = time.monotonic() + self.timeout
        self._drain_until(request_id, pending, deadline, method)
        with self._lock:
            self._pending.pop(request_id, None)

        if pending.error is not None:
            raise MCPTransportError(
                f"MCP server returned an error for {method!r}: {pending.error}"
            )
        if pending.response is None:
            raise MCPTimeoutError(
                f"MCP server did not answer {method!r} within {self.timeout}s"
                f"{self._stderr_hint()}"
            )
        return pending.response

    def _drain_until(
        self,
        request_id: int,
        pending: _Pending,
        deadline: float,
        method: str,
    ) -> None:
        """Consume stdout lines until our response arrives or the deadline passes."""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPTimeoutError(
                    f"MCP server did not answer {method!r} within {self.timeout}s"
                    f"{self._stderr_hint()}"
                )
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                raise MCPTimeoutError(
                    f"MCP server did not answer {method!r} within {self.timeout}s"
                    f"{self._stderr_hint()}"
                ) from None
            if line is None:
                raise MCPTransportError(
                    f"MCP server exited before answering {method!r}"
                    f"{self._stderr_hint()}"
                )
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                # A server that prints debug output to stdout is broken but not
                # fatal: skip the line rather than fail the call.
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("id") != request_id:
                # A notification or an out-of-order response: ignore. We only ever
                # have one request in flight at a time, so this is safe.
                continue
            if "error" in payload:
                pending.error = payload["error"]
            else:
                pending.response = payload
            pending.event.set()
            return

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    # ------------------------------------------------------------------- MCP
    def initialize(self) -> dict[str, Any]:
        """Perform the MCP handshake. Idempotent."""
        response = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": self.client_name, "version": self.client_version},
            },
        )
        self._notify("notifications/initialized")
        result: dict[str, Any] = response.get("result") or {}
        return result

    def list_tools(self) -> list[ToolDefinition]:
        self.initialize()
        if self._tools is not None:
            return list(self._tools)
        response = self._request("tools/list")
        raw = response.get("result", {}).get("tools", [])
        tools: list[ToolDefinition] = []
        for item in raw:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            tools.append(
                ToolDefinition(
                    name=str(item["name"]),
                    description=str(item.get("description", "")),
                    input_schema=dict(item.get("inputSchema") or {"type": "object"}),
                )
            )
        self._tools = tools
        return list(tools)

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute one tool. Transport failures raise; tool failures return ok=False."""
        started = time.perf_counter()
        self.initialize()
        response = self._request(
            "tools/call", {"name": tool_name, "arguments": dict(arguments)}
        )
        elapsed = (time.perf_counter() - started) * 1000.0
        result = response.get("result", {})
        content = result.get("content")
        is_error = bool(result.get("isError", False))
        text = _flatten_content(content)
        return ToolResult(
            tool_name=tool_name,
            ok=not is_error,
            content=text if is_error else (text if text is not None else content),
            error=text if is_error else None,
            duration_ms=elapsed,
            meta={"is_error": is_error},
        )

    # --------------------------------------------------------------- lifecycle
    def close(self) -> None:
        """Terminate the child process. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)
        self._proc = None

    def __enter__(self) -> "StdioMCPConnector":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - best-effort cleanup
        try:
            self.close()
        except Exception:  # noqa: BLE001 - a destructor must never raise
            pass


def _flatten_content(content: Any) -> Any:
    """Turn MCP's content blocks into something the model can read.

    MCP returns ``[{"type": "text", "text": "..."}]``. Passing that structure
    through unchanged would make every prompt noisier for no benefit, so text
    blocks are concatenated and non-text blocks are kept as-is.
    """
    if content is None:
        return None
    if not isinstance(content, list):
        return content
    texts: list[str] = []
    others: list[Any] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            texts.append(str(block.get("text", "")))
        else:
            others.append(block)
    if texts and not others:
        return "\n".join(texts)
    if texts:
        return {"text": "\n".join(texts), "other": others}
    return others
