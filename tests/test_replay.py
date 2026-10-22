"""Replay tests: recording, playback, and the guarantees that make replay useful.

The central test is :func:`test_replay_runs_the_real_guards_not_a_frozen_verdict`.
It records a *passing* run, then replays it against a tightened output contract. If
replay were faking the verdict, the replay would still pass. Because replay only
replaces the model and the transport, the real validator rejects the payload and the
replay fails — which is the property that makes a cassette a regression tool rather
than a recording of conclusions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness_core.adapters.llm import FakeLLMAdapter
from harness_core.engine import HarnessOrchestrationEngine
from harness_core.guards import BusinessRule, OutputGuard
from harness_core.observability import StructuredLogger
from harness_core.replay import (
    CASSETTE_VERSION,
    Cassette,
    CassetteMismatch,
    Recorder,
    RecordingLLMAdapter,
    RecordingMCPConnector,
    ReplayError,
    ReplayExhausted,
    ReplayLLMAdapter,
    ReplayMCPConnector,
    summarize,
)
from harness_core.scenarios import (
    ORDER_OUTPUT_SCHEMA,
    build_connector,
    build_output_guard,
    build_tool_specs,
)
from harness_core.state import RunStatus

from .helpers import VALID, response_for, tool_response


# ---------------------------------------------------------------------------
# cassette format
# ---------------------------------------------------------------------------


def test_cassette_roundtrip(tmp_path: Path) -> None:
    recorder = Recorder()
    recorder.record_llm(response_for())
    recorder.record_tool(
        __import__("harness_core.adapters.mcp", fromlist=["ToolResult"]).ToolResult(
            "fetch_order", True, {"ok": 1}
        )
    )
    path = recorder.save(tmp_path / "c.jsonl")

    loaded = Cassette.load(path)
    assert loaded.count() == 2
    assert loaded.count("llm") == 1
    assert loaded.count("tool") == 1
    assert loaded.entries[0].kind == "llm"
    assert loaded.entries[1].payload["tool_name"] == "fetch_order"


def test_cassette_file_starts_with_a_version_marker(tmp_path: Path) -> None:
    recorder = Recorder()
    recorder.record_llm(response_for())
    path = recorder.save(tmp_path / "c.jsonl")
    first = path.read_text(encoding="utf-8").splitlines()[0]
    assert json.loads(first)["cassette_version"] == CASSETTE_VERSION


def test_cassette_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ReplayError, match="not found"):
        Cassette.load(tmp_path / "nope.jsonl")


def test_cassette_invalid_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text('{"kind": "llm", "seq": 0}\nnot json\n', encoding="utf-8")
    with pytest.raises(ReplayError, match="invalid JSON"):
        Cassette.load(path)


def test_cassette_entry_without_seq_raises_replay_error(tmp_path: Path) -> None:
    path = tmp_path / "truncated.jsonl"
    path.write_text('{"kind": "llm"}\n', encoding="utf-8")
    with pytest.raises(ReplayError, match="missing 'seq'"):
        Cassette.load(path)


def test_cassette_rewind_resets_cursors() -> None:
    cassette = Cassette(
        [
            __import__("harness_core.replay", fromlist=["CassetteEntry"]).CassetteEntry(
                "llm", 0, {"response": response_for().to_dict()}
            )
        ]
    )
    first = cassette.next_entry("llm")
    assert first.kind == "llm"
    with pytest.raises(ReplayExhausted):
        cassette.next_entry("llm")
    cassette.rewind()
    assert cassette.next_entry("llm").kind == "llm"


def test_exhausted_cassette_raises_instead_of_defaulting() -> None:
    """Silently returning a default would turn 'incomplete' into 'passed'."""
    cassette = Cassette([])
    with pytest.raises(ReplayExhausted, match="incomplete"):
        cassette.next_entry("llm")


def test_summarize_counts_kinds_and_tools() -> None:
    recorder = Recorder()
    recorder.record_llm(response_for())
    recorder.record_tool(__import__("harness_core.adapters.mcp", fromlist=["ToolResult"]).ToolResult("a", True))
    recorder.record_tool(__import__("harness_core.adapters.mcp", fromlist=["ToolResult"]).ToolResult("b", True))
    recorder.record_tool(__import__("harness_core.adapters.mcp", fromlist=["ToolResult"]).ToolResult("a", True))
    stats = summarize(recorder.to_cassette())
    assert stats == {
        "entries": 4,
        "llm_calls": 1,
        "tool_calls": 3,
        "tools": ["a", "b"],
    }


# ---------------------------------------------------------------------------
# recording wrappers are transparent
# ---------------------------------------------------------------------------


def test_recording_llm_is_transparent() -> None:
    inner = FakeLLMAdapter([response_for()])
    recorder = Recorder()
    wrapped = RecordingLLMAdapter(inner, recorder)

    from harness_core.state import AgentHarnessState

    state = AgentHarnessState(task_prompt="t", tool_whitelist=[])
    response = wrapped.generate("prompt", state)
    assert response.content == VALID
    assert recorder.count_kind("llm") == 1 if hasattr(recorder, "count_kind") else True
    assert len(recorder.entries) == 1
    # Delegation keeps the wrapped adapter observable.
    assert wrapped.prompts == ["prompt"]


def test_recording_connector_is_transparent(connector) -> None:
    recorder = Recorder()
    wrapped = RecordingMCPConnector(connector, recorder)
    result = wrapped.call_tool("fetch_order", {"order_id": "ORD-1001"})
    assert result.ok
    assert len(recorder.entries) == 1
    assert recorder.entries[0].payload["tool_name"] == "fetch_order"
    # list_tools must not be recorded: it is not a side effect worth replaying.
    wrapped.list_tools()
    assert len(recorder.entries) == 1


# ---------------------------------------------------------------------------
# end-to-end: record then replay
# ---------------------------------------------------------------------------


def _engine(llm, connector, guard=None, max_retries: int = 3) -> HarnessOrchestrationEngine:
    return HarnessOrchestrationEngine(
        llm=llm,
        connector=connector,
        output_guard=guard or build_output_guard(),
        tool_specs=build_tool_specs(),
        high_risk_tools={"write_api", "delete_resource"},
        max_retries=max_retries,
        logger=StructuredLogger(enabled=False),
    )


def test_record_then_replay_reproduces_the_outcome(tmp_path: Path) -> None:
    responses = [tool_response("fetch_order", {"order_id": "ORD-1001"}), response_for()]
    recorder = Recorder()
    engine = _engine(
        RecordingLLMAdapter(FakeLLMAdapter(responses), recorder),
        RecordingMCPConnector(build_connector(), recorder),
    )
    original = engine.run("settle", tool_whitelist=["fetch_order"])
    path = recorder.save(tmp_path / "c.jsonl")

    cassette = Cassette.load(path)
    replay_engine = _engine(ReplayLLMAdapter(cassette), ReplayMCPConnector(cassette))
    replayed = replay_engine.run("settle", tool_whitelist=["fetch_order"])

    assert replayed.status is original.status
    assert replayed.attempts == original.attempts
    assert replayed.execution_result == original.execution_result
    # A replay is a new run: the trace id must NOT be reused.
    assert replayed.trace_id != original.trace_id


def test_replay_runs_the_real_guards_not_a_frozen_verdict(tmp_path: Path) -> None:
    """The property that makes a cassette a regression tool.

    The recording is a *passing* run. On replay the output contract is tightened so
    the recorded payload no longer satisfies it. A verdict-recording replay would
    still pass; an adapter-boundary replay must fail.
    """
    responses = [response_for()]
    recorder = Recorder()
    engine = _engine(RecordingLLMAdapter(FakeLLMAdapter(responses), recorder), build_connector())
    original = engine.run("settle", tool_whitelist=["fetch_order"])
    assert original.ok
    path = recorder.save(tmp_path / "c.jsonl")

    tightened = OutputGuard(
        output_schema=ORDER_OUTPUT_SCHEMA,
        rules=[BusinessRule(name="impossible", op="gt", field="amount", value=999_999)],
    )
    cassette = Cassette.load(path)
    replay_engine = _engine(
        ReplayLLMAdapter(cassette),
        ReplayMCPConnector(cassette),
        guard=tightened,
        max_retries=1,
    )
    replayed = replay_engine.run("settle", tool_whitelist=["fetch_order"])

    assert not replayed.ok, "the replay must re-run the validator, not trust the recording"
    assert replayed.status is RunStatus.FAILED_WITH_CIRCUIT_BREAKER
    assert replayed.error_stack is not None
    assert any(
        "impossible" in finding.failed_gate for finding in replayed.error_stack.blocking
    )


def test_replay_reproduces_the_self_healing_chain(tmp_path: Path) -> None:
    broken = {**VALID, "currency": "EUR"}
    responses = [response_for(broken), response_for()]
    recorder = Recorder()
    engine = _engine(RecordingLLMAdapter(FakeLLMAdapter(responses), recorder), build_connector())
    original = engine.run("settle", tool_whitelist=["fetch_order"])
    assert original.status is RunStatus.SELF_HEALED
    path = recorder.save(tmp_path / "c.jsonl")

    cassette = Cassette.load(path)
    replayed = _engine(ReplayLLMAdapter(cassette), ReplayMCPConnector(cassette)).run(
        "settle", tool_whitelist=["fetch_order"]
    )
    assert replayed.status is RunStatus.SELF_HEALED
    assert replayed.attempts == original.attempts
    assert replayed.state is not None
    assert replayed.state.retry_count == 1


def test_replay_reproduces_a_tool_failure(tmp_path: Path) -> None:
    def explode(_args):
        raise RuntimeError("upstream 503")

    connector = build_connector()
    connector.add_tool("fetch_order", explode)
    responses = [tool_response("fetch_order", {"order_id": "ORD-1"}), response_for()]
    recorder = Recorder()
    engine = _engine(RecordingLLMAdapter(FakeLLMAdapter(responses), recorder),
                     RecordingMCPConnector(connector, recorder))
    original = engine.run("settle", tool_whitelist=["fetch_order"])
    path = recorder.save(tmp_path / "c.jsonl")

    cassette = Cassette.load(path)
    replayed = _engine(ReplayLLMAdapter(cassette), ReplayMCPConnector(cassette)).run(
        "settle", tool_whitelist=["fetch_order"]
    )
    assert replayed.status is original.status
    assert replayed.status is RunStatus.SELF_HEALED


def test_replay_detects_a_tool_name_mismatch(tmp_path: Path) -> None:
    recorder = Recorder()
    recorder.record_tool(
        __import__("harness_core.adapters.mcp", fromlist=["ToolResult"]).ToolResult(
            "fetch_order", True, {"order_id": "ORD-1"}
        )
    )
    cassette = recorder.to_cassette()
    connector = ReplayMCPConnector(cassette)
    with pytest.raises(CassetteMismatch, match="expected tool"):
        connector.call_tool("write_api", {})


def test_replay_connector_advertises_only_recorded_tools(tmp_path: Path) -> None:
    recorder = Recorder()
    recorder.record_tool(
        __import__("harness_core.adapters.mcp", fromlist=["ToolResult"]).ToolResult(
            "fetch_order", True, {}
        )
    )
    connector = ReplayMCPConnector(recorder.to_cassette())
    names = [tool.name for tool in connector.list_tools()]
    assert names == ["fetch_order"], "a replay must not advertise unused capabilities"


def test_replay_of_an_incomplete_cassette_fails_loudly(tmp_path: Path) -> None:
    """Record a tool call but drop its result: the replay must refuse to invent one.

    Note the shape: recording a single ``llm`` entry is *not* incomplete, because
    ``FakeLLMAdapter`` repeats its last response and the run can finish in one turn.
    The genuinely missing piece is a tool result the run depends on.
    """
    recorder = Recorder()
    recorder.record_llm(tool_response("fetch_order", {"order_id": "ORD-1001"}))
    recorder.record_llm(response_for())
    # Deliberately no "tool" entry.
    path = recorder.save(tmp_path / "c.jsonl")

    cassette = Cassette.load(path)
    engine = _engine(ReplayLLMAdapter(cassette), ReplayMCPConnector(cassette))
    with pytest.raises(ReplayExhausted, match="incomplete"):
        engine.run("settle", tool_whitelist=["fetch_order"])


def test_exhausted_llm_cassette_escapes_the_self_healing_loop(tmp_path: Path) -> None:
    """The LLM-side exhaustion must raise, not become a BLOCK finding.

    Without the ``except HarnessInfrastructureError: raise`` guard at the adapter
    boundary, an exhausted cassette is caught by the generic handler, turned into an
    ``llm.error`` finding, retried ``max_retries`` times, and finally reported as a
    plausible-looking FAILED run. A broken fixture would then masquerade as a real
    verdict — which is precisely what a CI gate must never do.
    """
    recorder = Recorder()
    # No llm entries at all: the very first generate() call has nothing to serve.
    recorder.record_tool(
        __import__("harness_core.adapters.mcp", fromlist=["ToolResult"]).ToolResult(
            "fetch_order", True, {"order_id": "ORD-1001"}
        )
    )
    path = recorder.save(tmp_path / "c.jsonl")

    cassette = Cassette.load(path)
    engine = _engine(ReplayLLMAdapter(cassette), ReplayMCPConnector(cassette), max_retries=3)
    with pytest.raises(ReplayExhausted, match="incomplete"):
        engine.run("settle", tool_whitelist=["fetch_order"])


def test_infrastructure_error_is_not_converted_into_a_finding() -> None:
    """Direct check on the boundary: the exception must not be swallowed."""

    class ExplodingLLM:
        name = "exploding"

        def generate(self, prompt, state, *, tools=None):
            raise ReplayExhausted("cassette has 0 'llm' entries but the run asked for #1")

    engine = HarnessOrchestrationEngine(
        llm=ExplodingLLM(),
        connector=build_connector(),
        output_guard=build_output_guard(),
        tool_specs=build_tool_specs(),
        max_retries=3,
        logger=StructuredLogger(enabled=False),
    )
    with pytest.raises(ReplayExhausted):
        engine.run("settle", tool_whitelist=["fetch_order"])


def test_replay_with_the_graph_driver_matches_while(tmp_path: Path) -> None:
    broken = {**VALID, "currency": "EUR"}
    responses = [response_for(broken), response_for()]
    recorder = Recorder()
    engine = _engine(RecordingLLMAdapter(FakeLLMAdapter(responses), recorder), build_connector())
    engine.run("settle", tool_whitelist=["fetch_order"])
    path = recorder.save(tmp_path / "c.jsonl")

    outcomes = {}
    for driver in ("while", "graph"):
        cassette = Cassette.load(path)
        replayed = _engine(ReplayLLMAdapter(cassette), ReplayMCPConnector(cassette)).run(
            "settle", tool_whitelist=["fetch_order"], driver=driver
        )
        outcomes[driver] = (replayed.status, replayed.attempts)
    assert outcomes["while"] == outcomes["graph"]
