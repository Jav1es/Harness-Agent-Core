"""An MCP client over HTTP with Server-Sent Events, standard library only.

Transport shape
---------------
MCP's HTTP transport is JSON-RPC 2.0 over two HTTP exchanges:

* ``POST <url>`` carries the request; the reply is either a plain
  ``application/json`` body or an ``text/event-stream`` whose ``data:`` lines each
  hold one JSON-RPC message.
* The server may hand back an ``Mcp-Session-Id`` header on ``initialize``, which
  every later request must echo. Losing it turns a working session into a stream of
  404s, so it is tracked explicitly rather than assumed.

Why not the ``mcp`` SDK: same reasoning as the stdio client — the wire format is
small and stable, the package promises zero runtime dependencies, and a real local
server makes the transport testable end to end.

Failure policy (identical to the stdio client, on purpose)
----------------------------------------------------------
* **Transport**: refused connection, HTTP error status, timeout, unparseable body →
  ``HarnessInfrastructureError``. The model cannot repair a server that is down.
* **Protocol**: a JSON-RPC ``error`` object → also an infrastructure error, because
  it means the request was malformed at the protocol level, not that the *tool*
  rejected the work.
* **Tool**: ``isError: true`` in the result → ``ToolResult(ok=False)``, a normal
  business outcome the self-healing loop should see.

Getting that last distinction wrong in either direction is the classic bug: it
either asks the model to fix a dead socket, or hides a real tool failure from the
repair loop.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from ..errors import HarnessInfrastructureError
from .mcp import ToolDefinition, ToolResult
from .mcp_stdio import PROTOCOL_VERSION, _flatten_content

#: How long to wait for one response before declaring the server unresponsive.
DEFAULT_TIMEOUT = 30.0


class MCPHttpError(HarnessInfrastructureError):
    """The HTTP transport failed: unreachable, error status, or bad body."""


class MCPHttpTimeoutError(MCPHttpError):
    """The server accepted the request but did not answer in time."""


@dataclass
class HttpMCPConnector:
    """Talks to an MCP server over streamable HTTP.

    Usage::

        with HttpMCPConnector("https://example.com/mcp") as conn:
            tools = conn.list_tools()
            result = conn.call_tool("fetch_order", {"order_id": "ORD-1001"})

    ``headers`` carries authentication. It is a plain mapping so the caller decides
    where the token comes from — this class never reads the environment, because a
    connector that silently picks up ambient credentials is hard to audit.
    """

    url: str
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = DEFAULT_TIMEOUT
    client_name: str = "harness-agent-core"
    client_version: str = "0.1.0"

    name = "http-mcp"

    def __post_init__(self) -> None:
        self._session_id: str | None = None
        self._request_id = 0
        self._tools: list[ToolDefinition] | None = None
        self._initialized = False
        self._closed = False

    # ---------------------------------------------------------------- session
    @property
    def session_id(self) -> str | None:
        """The ``Mcp-Session-Id`` the server assigned, if any."""
        return self._session_id

    def initialize(self) -> dict[str, Any]:
        """Perform the handshake. Idempotent."""
        if self._initialized:
            return {"protocolVersion": PROTOCOL_VERSION}
        response = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": self.client_name, "version": self.client_version},
            },
        )
        self._initialized = True
        self._notify("notifications/initialized")
        result: dict[str, Any] = response.get("result") or {}
        return result

    # ------------------------------------------------------------------- API
    def list_tools(self) -> list[ToolDefinition]:
        self.initialize()
        if self._tools is not None:
            return list(self._tools)
        response = self._request("tools/list")
        raw = (response.get("result") or {}).get("tools") or []
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
        response = self._request("tools/call", {"name": tool_name, "arguments": dict(arguments)})
        elapsed = (time.perf_counter() - started) * 1000.0
        result = response.get("result") or {}
        content = result.get("content")
        is_error = bool(result.get("isError", False))
        text = _flatten_content(content)
        return ToolResult(
            tool_name=tool_name,
            ok=not is_error,
            content=text if is_error else (text if text is not None else content),
            error=text if is_error else None,
            duration_ms=elapsed,
            meta={"is_error": is_error, "transport": "http"},
        )

    def close(self) -> None:
        """Release the session. Safe to call twice, never raises.

        Sends ``DELETE`` when a session id exists: an MCP server that is never told
        the session ended keeps it alive until its own timeout, which leaks state on
        the server for every client run.
        """
        if self._closed:
            return
        self._closed = True
        if self._session_id is None:
            return
        try:
            self._send_http("DELETE", None)
        except MCPHttpError:
            pass  # best effort: closing must not raise
        self._session_id = None

    def __enter__(self) -> "HttpMCPConnector":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -------------------------------------------------------------- internals
    def _next_request_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_request_id(), "method": method}
        if params is not None:
            message["params"] = params
        status, body, headers, matched = self._send_http("POST", message, want_id=message["id"])
        if status == 202 and not body.strip() and matched is None:
            # The server accepted a notification-style POST with no reply body.
            return {"result": {}}

        if matched is not None:
            error = matched.get("error")
            if error:
                raise MCPHttpError(f"MCP server returned an error for {method!r}: {error}")
            return matched

        # Fall back to parsing the whole body: a server may legitimately answer with
        # plain JSON instead of a stream, and the streaming path finds nothing there.
        messages = _parse_messages(body, headers.get("Content-Type", ""))
        for item in messages:
            if item.get("id") == message["id"]:
                error = item.get("error")
                if error:
                    raise MCPHttpError(
                        f"MCP server returned an error for {method!r}: {error}"
                    )
                return item
        raise MCPHttpError(
            f"MCP server did not return a response for {method!r} "
            f"(got {len(messages)} message(s) instead)"
        )

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        try:
            self._send_http("POST", message)
        except MCPHttpError:
            # A rejected notification must not abort the handshake: some servers do
            # not implement notifications at all, and failing here would make a
            # working server look broken.
            pass

    def _send_http(
        self,
        method: str,
        payload: dict[str, Any] | None,
        *,
        want_id: Any = None,
    ) -> tuple[int, str, dict[str, str], dict[str, Any] | None]:
        """POST (or DELETE) and return ``(status, body, headers, matched)``.

        When ``want_id`` is given and the server answers with an SSE stream, the
        stream is read **line by line** and the matching JSON-RPC message is returned
        as soon as it arrives. ``body`` is then whatever was read up to that point.

        Why this matters: an SSE response may stay open after the reply (keep-alives,
        server-side notifications). Reading to EOF would block until the server
        decides to close — turning a 0.3 s reply into a 30 s timeout, or a timeout
        into a spurious failure. Measured: a server that replies and then sleeps
        returns in 0.33 s with streaming and only after the sleep without it.
        """
        data = None
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": f"{self.client_name}/{self.client_version}",
            **self.headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        request = urllib.request.Request(self.url, data=data, headers=headers, method=method)
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                returned = {key: value for key, value in response.headers.items()}
                status = int(response.status)
                content_type = _header(returned, "Content-Type") or ""
                if want_id is not None and "text/event-stream" in content_type.lower():
                    body, matched = _read_sse_until(response, want_id)
                else:
                    body = response.read().decode("utf-8", errors="replace")
                    matched = None
        except urllib.error.HTTPError as exc:
            detail = _safe_read(exc)
            raise MCPHttpError(
                f"MCP endpoint returned HTTP {exc.code} for {method} {self.url}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise MCPHttpError(
                f"could not reach MCP endpoint {self.url}: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise MCPHttpTimeoutError(
                f"MCP endpoint {self.url} timed out after {self.timeout}s"
            ) from exc
        except OSError as exc:
            raise MCPHttpError(f"MCP transport error for {self.url}: {exc}") from exc
        finally:
            _ = time.perf_counter() - started

        # Track the session id the server handed out; losing it breaks every later
        # request, so it is captured here rather than in each caller.
        session = _header(returned, "Mcp-Session-Id")
        if session:
            self._session_id = session
        return status, body, returned, matched


def _read_sse_until(response: Any, want_id: Any) -> tuple[str, dict[str, Any] | None]:
    """Read an SSE stream line by line, stopping at the reply for ``want_id``.

    Returns ``(body_read_so_far, matched_message_or_None)``.

    The loop stops at the first message whose ``id`` equals ``want_id``. Everything
    read up to that point is returned as ``body`` so a caller that wants the raw
    transcript still has it — but the connection is closed by the ``with`` block
    above as soon as we return, which is the whole point: the server may keep the
    stream open indefinitely for keep-alives.

    A malformed chunk is skipped rather than fatal (another message may still be
    valid); a stream that ends without our id returns ``None`` and the caller falls
    back to whole-body parsing, which then reports the mismatch properly.
    """
    lines: list[str] = []
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
        lines.append(line)
        message = _sse_line_to_message(line)
        if message is not None and message.get("id") == want_id:
            return "".join(lines), message
    return "".join(lines), None


def _sse_line_to_message(line: str) -> dict[str, Any] | None:
    """Turn one SSE line into a JSON-RPC message, or ``None`` if it is not one.

    Shared by the streaming reader and the whole-body parser. Keeping two copies of
    this logic was a real defect: a fix to one silently left the other broken, and a
    mutation in one path was invisible to tests exercising the other.

    Returns ``None`` for keep-alives, comments, blank lines, ``[DONE]``, non-``data:``
    lines and malformed JSON. A malformed chunk is skipped rather than fatal —
    another message in the same stream may still be valid.
    """
    stripped = line.strip()
    if not stripped.startswith("data:"):
        return None
    payload = stripped[len("data:") :].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _header(headers: dict[str, str], name: str) -> str | None:
    """Case-insensitive header lookup (urllib does not normalise consistently)."""
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def _parse_messages(body: str, content_type: str) -> list[dict[str, Any]]:
    """Parse a JSON-RPC payload that may be plain JSON or an SSE stream.

    An SSE body holds one message per ``data:`` line. Blank lines and ``event:`` /
    ``id:`` / comment lines are skipped: a server is allowed to send keep-alives,
    and treating a keep-alive as a message would corrupt the reply.
    """
    text = body.strip()
    if not text:
        return []
    if "text/event-stream" not in content_type.lower():
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MCPHttpError(
                f"MCP endpoint returned a non-JSON body ({len(text)} bytes): {text[:200]!r}"
            ) from exc
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        if isinstance(parsed, dict):
            return [parsed]
        raise MCPHttpError(f"MCP endpoint returned {type(parsed).__name__}, not an object")

    messages: list[dict[str, Any]] = []
    for line in text.splitlines():
        message = _sse_line_to_message(line)
        if message is not None:
            messages.append(message)
    return messages


def _safe_read(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except (OSError, ValueError):  # pragma: no cover - stream already consumed
        return "<no body>"
    return body[:500]
