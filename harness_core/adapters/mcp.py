"""MCP-style connectors.

The harness only knows the :class:`MCPConnector` protocol: ``list_tools`` to
discover capabilities and ``call_tool`` to execute one. It never reaches into a
transport, which is what makes ``FakeMCPConnector`` a first-class citizen rather
than a mock bolted on for tests.

A real stdio client lives in :mod:`harness_core.adapters.mcp_stdio` — it speaks
newline-delimited JSON-RPC 2.0 to a child process and needs no third-party SDK.
"""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence, runtime_checkable


@dataclass
class ToolDefinition:
    """A capability advertised by an MCP server."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass
class ToolResult:
    """Outcome of one tool execution.

    ``ok=False`` is a *normal* outcome: the engine turns it into a BLOCK finding
    and lets the self-healing loop try to repair it. Exceptions never escape the
    connector.
    """

    tool_name: str
    ok: bool
    content: Any = None
    error: str | None = None
    duration_ms: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "ok": self.ok,
            "content": self.content,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 3),
            "meta": self.meta,
        }


@runtime_checkable
class MCPConnector(Protocol):
    """Transport-agnostic tool provider (stdio MCP, HTTP, in-process fake)."""

    name: str

    def list_tools(self) -> list[ToolDefinition]: ...

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult: ...

    def close(self) -> None: ...


class FakeMCPConnector:
    """In-process connector backed by plain Python callables.

    Handlers receive the parsed arguments and may return any JSON-serialisable
    value; raising an exception becomes ``ok=False`` with the exception message,
    which the self-healing loop then feeds back to the model.
    """

    name = "fake-mcp"

    def __init__(
        self,
        handlers: dict[str, Callable[[dict[str, Any]], Any]] | None = None,
        tools: Sequence[ToolDefinition] | None = None,
    ) -> None:
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = dict(handlers or {})
        self._tools: dict[str, ToolDefinition] = {}
        for definition in tools or []:
            self.register(definition, self._handlers.get(definition.name))
        for name in self._handlers:
            self._tools.setdefault(name, ToolDefinition(name=name))
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    # ------------------------------------------------------------ registry
    def register(
        self,
        definition: ToolDefinition,
        handler: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self._tools[definition.name] = definition
        if handler is not None:
            self._handlers[definition.name] = handler

    def add_tool(
        self,
        name: str,
        handler: Callable[[dict[str, Any]], Any],
        *,
        description: str = "",
        input_schema: dict[str, Any] | None = None,
    ) -> None:
        self.register(
            ToolDefinition(
                name=name,
                description=description,
                input_schema=input_schema or {"type": "object"},
            ),
            handler,
        )

    # ------------------------------------------------------------------ API
    def list_tools(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        started = time.perf_counter()
        self.calls.append((tool_name, dict(arguments)))
        handler = self._handlers.get(tool_name)
        if handler is None:
            return ToolResult(
                tool_name=tool_name,
                ok=False,
                error=f"tool {tool_name!r} is not implemented by connector {self.name}",
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )
        try:
            content = handler(arguments)
        except Exception as exc:  # noqa: BLE001 - the connector boundary must not leak
            return ToolResult(
                tool_name=tool_name,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=(time.perf_counter() - started) * 1000.0,
                meta={"traceback": traceback.format_exc(limit=3)},
            )
        return ToolResult(
            tool_name=tool_name,
            ok=True,
            content=content,
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )

    def close(self) -> None:
        self.closed = True

    # -------------------------------------------------------------- helpers
    def call_count(self, tool_name: str | None = None) -> int:
        if tool_name is None:
            return len(self.calls)
        return sum(1 for name, _ in self.calls if name == tool_name)

    def called_tools(self) -> list[str]:
        return [name for name, _ in self.calls]


class StdioMCPConnector:
    """Deprecated alias for the real client in :mod:`mcp_stdio`.

    Kept only so old imports keep working. The implementation moved because a
    skeleton that raised ``NotImplementedError`` was a second, contradictory answer
    to "how do I talk to an MCP server" — and two answers is one too many.
    """

    def __new__(cls, *args: Any, **kwargs: Any) -> Any:
        from .mcp_stdio import StdioMCPConnector as _Real

        return _Real(*args, **kwargs)
