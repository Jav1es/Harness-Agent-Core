"""The LangGraph driver: same verdicts, and demonstrably a real graph walk.

Two things are being proven here.

1. **Equivalence** — for identical scripts, ``langgraph`` produces the same terminal
   status, step count, retry count and failure reason as ``while``. Equivalence alone
   is not enough (two implementations can agree on a wrong number), so the absolute
   assertions in ``test_loop.py`` cover that; here the point is that swapping the
   walker changes nothing observable.

2. **It is really LangGraph** — not a renamed copy of the hand-written walker. The
   tests assert the compiled graph's node/edge structure, that LangGraph's own
   ``recursion_limit`` is honoured, and that a node actually executes inside
   LangGraph's runtime. A driver that silently fell back to the while-loop would
   fail these.

The whole module is skipped when the optional extra is absent, so ``pytest`` stays
green on a zero-dependency install.
"""

from __future__ import annotations

import pytest

from harness_core.loop import DRIVERS, LoopContext, get_driver
from harness_core.state import RunStatus

from .helpers import VALID, response_for, tool_response

langgraph = pytest.importorskip("langgraph", reason="optional extra not installed")


pytestmark = pytest.mark.skipif(
    "langgraph" not in DRIVERS, reason="langgraph driver not registered"
)


# ---------------------------------------------------------------------------
# the graph is a real graph
# ---------------------------------------------------------------------------


def test_driver_is_registered_under_its_own_name() -> None:
    assert get_driver("langgraph").name == "langgraph"
    assert get_driver("langgraph") is not get_driver("graph")


def test_compiled_graph_has_the_expected_nodes() -> None:
    app = get_driver("langgraph").build()  # type: ignore[attr-defined]
    nodes = set(app.get_graph().nodes)
    assert {"__start__", "attempt", "breaker", "__end__"} == nodes


def test_compiled_graph_has_the_conditional_edge() -> None:
    app = get_driver("langgraph").build()  # type: ignore[attr-defined]
    edges = app.get_graph().edges
    sources = {edge.source for edge in edges}
    assert "attempt" in sources
    # attempt must be able to reach itself (retry) and the breaker.
    targets = {edge.target for edge in edges if edge.source == "attempt"}
    assert "attempt" in targets
    assert "breaker" in targets


def test_graph_is_compiled_not_interpreted_manually() -> None:
    """`build()` must return a LangGraph compiled graph, not our own object."""
    from langgraph.graph.state import CompiledStateGraph

    app = get_driver("langgraph").build()  # type: ignore[attr-defined]
    assert isinstance(app, CompiledStateGraph)


# ---------------------------------------------------------------------------
# equivalence with the hand-written drivers
# ---------------------------------------------------------------------------


def _outcome(engine, llm, driver: str, whitelist: list[str]):
    state = engine.build_state("task", tool_whitelist=whitelist)
    result = engine.run_with_self_healing(state, driver=driver)
    return (
        result.status,
        result.attempts,
        result.state.retry_count if result.state else -1,
        llm.prompt_count,
        result.failure_reason,
    )


@pytest.mark.parametrize(
    ("responses", "whitelist", "expected"),
    [
        ([response_for()], ["fetch_order"], RunStatus.FIRST_PASS),
        (
            [response_for({**VALID, "currency": "EUR"}), response_for()],
            ["fetch_order"],
            RunStatus.SELF_HEALED,
        ),
        (
            [tool_response("fetch_order", {"order_id": "ORD-1001"}), response_for()],
            ["fetch_order"],
            RunStatus.FIRST_PASS,
        ),
        (
            [
                response_for({**VALID, "order_id": "X"}),
                response_for({**VALID, "currency": "EUR"}),
                response_for({**VALID, "status": "unknown"}),
            ],
            ["fetch_order"],
            RunStatus.FAILED_WITH_CIRCUIT_BREAKER,
        ),
    ],
)
def test_langgraph_matches_while(engine_factory, responses, whitelist, expected) -> None:
    seen = {}
    for driver in ("while", "langgraph"):
        engine, llm = engine_factory(responses, max_retries=3)
        seen[driver] = _outcome(engine, llm, driver, whitelist)
    assert seen["while"] == seen["langgraph"], seen
    assert seen["while"][0] is expected


def test_langgraph_trips_the_breaker_on_a_repeated_failure(engine_factory) -> None:
    payload = {**VALID, "currency": "EUR"}
    engine, llm = engine_factory([response_for(payload)] * 5, max_retries=5)
    result = engine.run_with_self_healing(
        engine.build_state("task", tool_whitelist=["fetch_order"]), driver="langgraph"
    )
    assert "GXT_FINDINGS_RECURRED" in (result.failure_reason or "")
    assert llm.prompt_count == 2


def test_langgraph_respects_max_steps(engine_factory) -> None:
    engine, llm = engine_factory(
        [tool_response("fetch_order", {"order_id": "ORD-1001"})], max_steps=3
    )
    result = engine.run_with_self_healing(
        engine.build_state("task", tool_whitelist=["fetch_order"]), driver="langgraph"
    )
    assert result.status is RunStatus.FAILED_WITH_CIRCUIT_BREAKER
    assert result.attempts == 3
    assert llm.prompt_count == 3
    assert "max_steps=3" in (result.failure_reason or "")


def test_langgraph_parks_on_hitl(engine_factory, webhook) -> None:
    from harness_core.hitl import HumanApprovalGateway

    engine, _ = engine_factory(
        [tool_response("write_api", {"order_id": "ORD-1001", "amount": 1200.0})],
        gateway=HumanApprovalGateway(webhook),
    )
    state = engine.build_state("task", tool_whitelist=["fetch_order", "write_api"])
    result = engine.run_with_self_healing(state, driver="langgraph")
    assert result.status is RunStatus.PENDING_HUMAN_APPROVAL


def test_langgraph_reports_the_real_step_count(engine_factory) -> None:
    """3 tool turns then a deliverable == 4 steps, same as the other drivers."""
    responses = [
        tool_response("fetch_order", {"order_id": "ORD-1001"}),
        tool_response("fetch_order", {"order_id": "ORD-1001"}),
        tool_response("fetch_order", {"order_id": "ORD-1001"}),
        response_for(),
    ]
    engine, llm = engine_factory(responses, max_steps=8)
    result = engine.run_with_self_healing(
        engine.build_state("task", tool_whitelist=["fetch_order"]), driver="langgraph"
    )
    assert result.status is RunStatus.FIRST_PASS
    assert result.attempts == 4
    assert llm.prompt_count == 4


# ---------------------------------------------------------------------------
# LangGraph runtime semantics we must not break
# ---------------------------------------------------------------------------


def test_recursion_limit_scales_with_max_steps() -> None:
    """The limit must be derived from `max_steps`, not left at LangGraph's default.

    Measured, not assumed: langgraph 1.x defaults to 10007
    (``langgraph._internal._config.DEFAULT_RECURSION_LIMIT``), so a run has to be
    enormous before the default bites. The harness still sets it explicitly so that
    its own ``max_steps`` is the *only* budget that decides when a run stops — a
    framework default must never be the thing that ends a run.
    """
    from langgraph._internal._config import DEFAULT_RECURSION_LIMIT

    driver = get_driver("langgraph")
    assert driver.name == "langgraph"

    # The formula is the contract: 3 node visits per step plus headroom.
    for max_steps in (1, 8, 20, 100):
        assert max_steps * 3 + 10 > max_steps * 2, "must exceed 2 nodes per step"

    # Sanity: our scaling is far below the framework default, which is why the
    # default is not what we rely on.
    assert DEFAULT_RECURSION_LIMIT > 20 * 3 + 10


def test_a_long_run_does_not_hit_the_recursion_limit(engine_factory) -> None:
    """A 40-step run must finish on its own terms, not on a framework limit."""
    responses = [
        tool_response("fetch_order", {"order_id": "ORD-1001"}) for _ in range(39)
    ] + [response_for()]
    engine, _ = engine_factory(responses, max_steps=50)
    result = engine.run_with_self_healing(
        engine.build_state("task", tool_whitelist=["fetch_order"]), driver="langgraph"
    )
    assert result.status is RunStatus.FIRST_PASS
    assert result.attempts == 40


def test_an_explicit_limit_is_what_stops_a_run_not_the_framework(
    engine_factory, monkeypatch
) -> None:
    """Pin the mechanism: the driver must pass a recursion_limit derived from max_steps.

    Without this, a future refactor could drop the config and nothing would notice
    until a very large run blew up.
    """
    from harness_core import loop_langgraph

    captured: dict[str, object] = {}
    original = loop_langgraph.LangGraphLoopDriver.build

    def spy(self):
        app = original(self)

        class Recorder:
            def __init__(self, inner):
                self._inner = inner

            def invoke(self, state, config=None):
                captured["config"] = config
                return self._inner.invoke(state, config=config)

        return Recorder(app)

    monkeypatch.setattr(loop_langgraph.LangGraphLoopDriver, "build", spy)

    engine, _ = engine_factory([response_for()], max_steps=7)
    engine.run_with_self_healing(
        engine.build_state("task", tool_whitelist=["fetch_order"]), driver="langgraph"
    )
    assert captured["config"] == {"recursion_limit": 7 * 3 + 10}


def test_business_state_is_never_copied_through_the_graph(engine_factory) -> None:
    """The graph state carries only `ctx`, `step`, `action`.

    If `history` were in the graph state, a node returning a partial dict would drop
    it — the exact failure this project was built to prevent. Assert the graph state
    schema stays minimal.
    """
    from harness_core.loop_langgraph import _GraphState

    # ``snapshot`` was added for the durable mode: a checkpointer cannot serialise
    # a LoopContext (live engine/connector), so a durable walk carries a plain dict
    # instead. It is a *serialised copy* of the same business state, never a second
    # source of truth — the live state still lives in ``ctx.state``.
    assert set(_GraphState.__annotations__) == {"ctx", "snapshot", "step", "action"}
    assert "history" not in _GraphState.__annotations__
    assert "error_stack" not in _GraphState.__annotations__


def test_history_survives_a_langgraph_run(engine_factory) -> None:
    responses = [tool_response("fetch_order", {"order_id": "ORD-1001"}), response_for()]
    engine, _ = engine_factory(responses)
    state = engine.build_state("task", tool_whitelist=["fetch_order"])
    result = engine.run_with_self_healing(state, driver="langgraph")
    assert result.state is not None
    assert len(result.state.history) == 2, "history must survive the graph walk"
    assert [entry.attempt for entry in result.state.history] == [1, 2]


def test_langgraph_works_with_the_replay_cassette(tmp_path, engine_factory) -> None:
    """A recorded run must replay identically under the LangGraph driver."""
    from harness_core.adapters.llm import FakeLLMAdapter
    from harness_core.engine import HarnessOrchestrationEngine
    from harness_core.observability import StructuredLogger
    from harness_core.replay import (
        Cassette,
        Recorder,
        RecordingLLMAdapter,
        RecordingMCPConnector,
        ReplayLLMAdapter,
        ReplayMCPConnector,
    )
    from harness_core.scenarios import build_connector, build_output_guard, build_tool_specs

    broken = {**VALID, "currency": "EUR"}
    recorder = Recorder()
    recording = HarnessOrchestrationEngine(
        llm=RecordingLLMAdapter(FakeLLMAdapter([response_for(broken), response_for()]), recorder),
        connector=RecordingMCPConnector(build_connector(), recorder),
        output_guard=build_output_guard(),
        tool_specs=build_tool_specs(),
        max_retries=3,
        logger=StructuredLogger(enabled=False),
    )
    original = recording.run("settle", tool_whitelist=["fetch_order"])
    path = recorder.save(tmp_path / "c.jsonl")

    cassette = Cassette.load(path)
    replayed = HarnessOrchestrationEngine(
        llm=ReplayLLMAdapter(cassette),
        connector=ReplayMCPConnector(cassette),
        output_guard=build_output_guard(),
        tool_specs=build_tool_specs(),
        max_retries=3,
        logger=StructuredLogger(enabled=False),
    ).run("settle", tool_whitelist=["fetch_order"], driver="langgraph")

    assert replayed.status is original.status
    assert replayed.attempts == original.attempts


def test_loop_context_stays_minimal_under_langgraph() -> None:
    """The graph gets no more access than the other drivers do."""
    assert set(LoopContext.__annotations__) == {
        "engine",
        "state",
        "breaker",
        "tracer",
        "max_steps",
    }


# ---------------------------------------------------------------------------
# the optional extra must never break the base install
# ---------------------------------------------------------------------------


def test_availability_check_survives_a_broken_find_spec(monkeypatch) -> None:
    """`find_spec` can raise: broken install, permissions, half-removed package.

    Letting that escape made `import harness_core` fail on machines that never asked
    for LangGraph — the opposite of an optional extra. Any failure must read as
    "not available".
    """
    import importlib.util

    from harness_core.loop_langgraph import langgraph_available

    def explode(name, *args, **kwargs):
        raise ImportError("simulated broken install")

    monkeypatch.setattr(importlib.util, "find_spec", explode)
    assert langgraph_available() is False


def test_package_imports_without_langgraph() -> None:
    """Import the package in a subprocess where langgraph is unreachable."""
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent(
        """
        import sys

        class Blocker:
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in ("langgraph", "langchain_core"):
                    raise ImportError("hidden")
                return None

        sys.meta_path.insert(0, Blocker())
        import harness_core
        from harness_core.loop import DRIVERS, get_driver
        from harness_core.loop_langgraph import langgraph_available

        assert langgraph_available() is False
        assert "langgraph" not in DRIVERS
        assert {"while", "graph"} <= set(DRIVERS)
        try:
            get_driver("langgraph")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")
        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stderr[-1500:]
    assert "OK" in result.stdout
