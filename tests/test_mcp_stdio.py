"""Real stdio MCP tests: a live subprocess speaking JSON-RPC 2.0.

These are not mocks. Each test spawns ``tests/mcp_server_stub.py`` and talks the
actual MCP wire protocol to it, so the connector is verified end to end: framing,
handshake, content flattening, error classification and process cleanup.

The failure modes matter as much as the happy path, and they are classified
differently on purpose:

* a **tool-level** failure (``isError: true``) is a normal business outcome →
  ``ToolResult(ok=False)``, which the self-healing loop feeds back to the model;
* a **transport-level** failure (server died, never answered, returned a JSON-RPC
  error) raises ``HarnessInfrastructureError`` — retrying cannot fix a missing
  binary, and asking the model to "repair" an environment problem wastes the budget.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from harness_core.adapters.mcp import MCPConnector
from harness_core.adapters.mcp_stdio import (
    PROTOCOL_VERSION,
    MCPTimeoutError,
    MCPTransportError,
    StdioMCPConnector,
    _flatten_content,
)
from harness_core.errors import HarnessInfrastructureError

STUB = str(Path(__file__).parent / "mcp_server_stub.py")


def connector(*flags: str, timeout: float = 15.0) -> StdioMCPConnector:
    return StdioMCPConnector(sys.executable, [STUB, *flags], timeout=timeout)


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_lists_tools_from_a_real_server() -> None:
    with connector() as conn:
        tools = conn.list_tools()
    assert [t.name for t in tools] == ["echo", "add", "fail", "boom"]
    echo = next(t for t in tools if t.name == "echo")
    assert echo.description == "Echo the given text back"
    assert echo.input_schema["required"] == ["text"]


def test_tool_schemas_survive_the_wire() -> None:
    with connector() as conn:
        add = next(t for t in conn.list_tools() if t.name == "add")
    assert add.input_schema["properties"]["a"] == {"type": "integer"}


def test_calls_a_tool_and_flattens_text_content() -> None:
    with connector() as conn:
        result = conn.call_tool("echo", {"text": "hello"})
    assert result.ok
    assert result.content == "hello"
    assert result.error is None
    assert result.duration_ms >= 0


def test_numeric_result_is_returned_as_text() -> None:
    with connector() as conn:
        result = conn.call_tool("add", {"a": 2, "b": 40})
    assert result.ok
    assert result.content == "42"


def test_tool_level_error_is_a_normal_outcome() -> None:
    """isError=true must NOT raise: the model gets a chance to repair."""
    with connector() as conn:
        result = conn.call_tool("fail", {})
    assert result.ok is False
    assert result.error == "this tool always fails"
    assert result.meta["is_error"] is True


def test_list_tools_is_cached_after_the_first_call() -> None:
    with connector() as conn:
        first = conn.list_tools()
        second = conn.list_tools()
    assert [t.name for t in first] == [t.name for t in second]


def test_initialize_is_idempotent() -> None:
    with connector() as conn:
        first = conn.initialize()
        second = conn.initialize()
    assert first["protocolVersion"] == PROTOCOL_VERSION
    assert second["protocolVersion"] == PROTOCOL_VERSION
    assert first["serverInfo"]["name"] == "stub-server"


# ---------------------------------------------------------------------------
# transport failures raise (and are classified as infrastructure)
# ---------------------------------------------------------------------------


def test_protocol_error_raises() -> None:
    """A JSON-RPC error is not a tool failure: the request never produced a result."""
    with connector() as conn:
        with pytest.raises(MCPTransportError, match="boom"):
            conn.call_tool("boom", {})


def test_transport_error_is_an_infrastructure_error() -> None:
    with connector() as conn:
        with pytest.raises(HarnessInfrastructureError):
            conn.call_tool("boom", {})


def test_server_that_crashes_on_start_raises() -> None:
    with pytest.raises(MCPTransportError):
        with connector("--crash") as conn:
            conn.list_tools()


def test_server_that_never_answers_times_out() -> None:
    with pytest.raises(MCPTimeoutError):
        with connector("--hang", timeout=1.0) as conn:
            conn.list_tools()


def test_timeout_is_also_an_infrastructure_error() -> None:
    with pytest.raises(HarnessInfrastructureError):
        with connector("--hang", timeout=1.0) as conn:
            conn.call_tool("echo", {"text": "x"})


def test_missing_binary_raises_a_clear_error() -> None:
    conn = StdioMCPConnector("definitely-not-a-real-binary-xyz", timeout=2.0)
    with pytest.raises(MCPTransportError, match="could not start"):
        conn.list_tools()


def test_garbage_on_stdout_is_skipped_not_fatal() -> None:
    """A server that prints debug noise is broken but still usable."""
    with connector("--garbage") as conn:
        assert [t.name for t in conn.list_tools()] == ["echo", "add", "fail", "boom"]


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


def test_close_terminates_the_child_process() -> None:
    conn = connector()
    conn.list_tools()
    proc = conn._proc
    assert proc is not None and proc.poll() is None
    conn.close()
    assert proc.poll() is not None, "the child must be reaped on close"


def test_close_is_idempotent() -> None:
    conn = connector()
    conn.list_tools()
    conn.close()
    conn.close()  # must not raise


def test_context_manager_closes() -> None:
    with connector() as conn:
        conn.list_tools()
        proc = conn._proc
    assert proc is not None and proc.poll() is not None


def test_connector_satisfies_the_protocol() -> None:
    assert isinstance(connector(), MCPConnector)


def test_connector_name_is_stable() -> None:
    assert connector().name == "stdio-mcp"


# ---------------------------------------------------------------------------
# content flattening
# ---------------------------------------------------------------------------


def test_flatten_single_text_block() -> None:
    assert _flatten_content([{"type": "text", "text": "hi"}]) == "hi"


def test_flatten_multiple_text_blocks_joins_with_newlines() -> None:
    assert _flatten_content(
        [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    ) == "a\nb"


def test_flatten_keeps_non_text_blocks() -> None:
    result = _flatten_content(
        [{"type": "text", "text": "look"}, {"type": "image", "data": "..."}]
    )
    assert result == {"text": "look", "other": [{"type": "image", "data": "..."}]}


def test_flatten_passes_through_none_and_scalars() -> None:
    assert _flatten_content(None) is None
    assert _flatten_content("plain") == "plain"


# ---------------------------------------------------------------------------
# the engine treats a stdio connector like any other
# ---------------------------------------------------------------------------


def test_engine_runs_against_a_real_mcp_server() -> None:
    """The whole point: swap the connector and nothing above it changes."""
    from harness_core.adapters.llm import FakeLLMAdapter
    from harness_core.engine import HarnessOrchestrationEngine
    from harness_core.guards import ToolSpec
    from harness_core.observability import StructuredLogger

    from .helpers import response_for, tool_response

    responses = [
        tool_response("add", {"a": 1, "b": 2}),
        response_for(),
    ]
    with connector() as conn:
        engine = HarnessOrchestrationEngine(
            llm=FakeLLMAdapter(responses),
            connector=conn,
            tool_specs={
                "add": ToolSpec(
                    "add",
                    "",
                    {
                        "type": "object",
                        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                        "required": ["a", "b"],
                    },
                )
            },
            logger=StructuredLogger(enabled=False),
        )
        result = engine.run("add two numbers", tool_whitelist=["add"])

    assert result.ok
    assert result.state is not None
    # `history` records *turns*, not tool calls, so assert on the two turns the
    # script prescribes: the tool call, then the deliverable.
    assert len(result.state.history) == 2
    assert [entry.status for entry in result.state.history] == ["RUNNING", "FIRST_PASS"]


def test_engine_reports_a_tool_level_failure_and_self_heals() -> None:
    """`fail` returns isError=true → BLOCK finding → retry → then deliver."""
    from harness_core.adapters.llm import FakeLLMAdapter
    from harness_core.engine import HarnessOrchestrationEngine
    from harness_core.guards import ToolSpec
    from harness_core.observability import StructuredLogger
    from harness_core.state import RunStatus

    from .helpers import response_for, tool_response

    responses = [
        tool_response("fail", {}),
        response_for(),
    ]
    with connector() as conn:
        engine = HarnessOrchestrationEngine(
            llm=FakeLLMAdapter(responses),
            connector=conn,
            tool_specs={"fail": ToolSpec("fail", "", {"type": "object"})},
            max_retries=3,
            logger=StructuredLogger(enabled=False),
        )
        result = engine.run("use the failing tool", tool_whitelist=["fail"])

    assert result.status is RunStatus.SELF_HEALED
    assert result.state is not None and result.state.retry_count == 1


def test_engine_does_not_retry_an_infrastructure_failure() -> None:
    """A dead server must escape the loop, not burn the retry budget."""
    from harness_core.adapters.llm import FakeLLMAdapter
    from harness_core.engine import HarnessOrchestrationEngine
    from harness_core.observability import StructuredLogger

    from .helpers import tool_response

    with connector("--crash") as conn:
        engine = HarnessOrchestrationEngine(
            llm=FakeLLMAdapter([tool_response("echo", {"text": "x"})]),
            connector=conn,
            max_retries=5,
            logger=StructuredLogger(enabled=False),
        )
        with pytest.raises(HarnessInfrastructureError):
            engine.run("call echo", tool_whitelist=["echo"])
