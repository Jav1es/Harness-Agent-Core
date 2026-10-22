"""Loop driver tests: the `while` and `graph` drivers must agree exactly.

This is the executable form of the "swap it for LangGraph later" claim. If the two
drivers ever produce different terminal statuses, retry counts or trace ids for the
same script, the abstraction has leaked and the README is lying.
"""

from __future__ import annotations

import pytest

from harness_core.loop import (
    DRIVERS,
    GraphLoopDriver,
    LoopContext,
    WhileLoopDriver,
    get_driver,
)
from harness_core.state import RunStatus

from .helpers import VALID, response_for, tool_response


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_both_drivers_are_registered() -> None:
    assert {"while", "graph"} <= set(DRIVERS)
    assert isinstance(DRIVERS["while"], WhileLoopDriver)
    assert isinstance(DRIVERS["graph"], GraphLoopDriver)


def test_langgraph_driver_is_registered_only_when_available() -> None:
    """The package must stay importable with zero dependencies.

    So the driver is registered conditionally. This pins the *conditional*, not the
    presence: it passes both with and without the optional extra installed.
    """
    from harness_core.loop_langgraph import langgraph_available

    assert ("langgraph" in DRIVERS) is langgraph_available()


def test_get_driver_rejects_unknown_names() -> None:
    with pytest.raises(ValueError, match="unknown loop driver"):
        get_driver("temporal")


def test_graph_edges_only_point_at_known_nodes() -> None:
    known = {"attempt", "breaker", None}
    for key, target in GraphLoopDriver.EDGES.items():
        assert target in known, f"edge {key!r} -> {target!r} is not a known node"


# ---------------------------------------------------------------------------
# equivalence: the same script must produce the same outcome
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
def test_both_drivers_reach_the_same_status(engine_factory, responses, whitelist, expected) -> None:
    seen = {}
    for driver in DRIVERS:
        engine, llm = engine_factory(responses, max_retries=3)
        seen[driver] = _outcome(engine, llm, driver, whitelist)
    assert seen["while"] == seen["graph"], seen
    assert seen["while"][0] is expected, seen


def test_both_drivers_trip_the_breaker_identically(engine_factory) -> None:
    payload = {**VALID, "currency": "EUR"}
    seen = {}
    for driver in DRIVERS:
        engine, llm = engine_factory([response_for(payload)] * 5, max_retries=5)
        seen[driver] = _outcome(engine, llm, driver, ["fetch_order"])
    assert seen["while"] == seen["graph"], seen
    assert "GXT_FINDINGS_RECURRED" in (seen["while"][4] or "")
    assert seen["while"][3] == 2  # only two model turns were spent


def test_both_drivers_respect_max_steps(engine_factory) -> None:
    seen = {}
    for driver in DRIVERS:
        engine, llm = engine_factory(
            [tool_response("fetch_order", {"order_id": "ORD-1001"})], max_steps=3
        )
        seen[driver] = _outcome(engine, llm, driver, ["fetch_order"])
    assert seen["while"] == seen["graph"], seen
    assert seen["while"][0] is RunStatus.FAILED_WITH_CIRCUIT_BREAKER
    assert seen["while"][3] == 3


# Equivalence alone is not enough: two drivers can agree on a wrong number. These
# absolute assertions pin the actual counts, so a mutant that shifts *both* drivers
# is still caught.


@pytest.mark.parametrize("driver", list(DRIVERS))
def test_driver_reports_the_real_step_count(engine_factory, driver: str) -> None:
    """3 tool turns then a deliverable == 4 steps, reported by both drivers."""
    responses = [
        tool_response("fetch_order", {"order_id": "ORD-1001"}),
        tool_response("fetch_order", {"order_id": "ORD-1001"}),
        tool_response("fetch_order", {"order_id": "ORD-1001"}),
        response_for(),
    ]
    engine, llm = engine_factory(responses, max_steps=8)
    state = engine.build_state("task", tool_whitelist=["fetch_order"])
    result = engine.run_with_self_healing(state, driver=driver)

    assert result.status is RunStatus.FIRST_PASS
    assert result.attempts == 4, f"{driver}: expected 4 steps, got {result.attempts}"
    assert llm.prompt_count == 4
    assert result.state is not None and result.state.retry_count == 0


@pytest.mark.parametrize("driver", list(DRIVERS))
def test_graph_driver_reports_the_real_step_count(engine_factory, driver: str) -> None:
    """A single attempt must report exactly 1 step, not 0 and not 2."""
    engine, _ = engine_factory([response_for()])
    state = engine.build_state("task", tool_whitelist=["fetch_order"])
    result = engine.run_with_self_healing(state, driver=driver)
    assert result.attempts == 1, f"{driver}: expected 1 step, got {result.attempts}"


def test_both_drivers_park_on_hitl(engine_factory, webhook) -> None:
    from harness_core.hitl import HumanApprovalGateway

    gateway = HumanApprovalGateway(webhook)
    seen = {}
    for driver in DRIVERS:
        engine, _ = engine_factory(
            [tool_response("write_api", {"order_id": "ORD-1001", "amount": 1200.0})],
            gateway=gateway,
        )
        state = engine.build_state("task", tool_whitelist=["fetch_order", "write_api"])
        seen[driver] = engine.run_with_self_healing(state, driver=driver).status
    assert seen["while"] is seen["graph"] is RunStatus.PENDING_HUMAN_APPROVAL


def test_drivers_agree_on_retry_count_for_the_same_script(engine_factory) -> None:
    seen = {}
    for driver in DRIVERS:
        engine, _ = engine_factory(
            [
                response_for({**VALID, "currency": "EUR"}),
                response_for({**VALID, "status": "unknown"}),
                response_for(),
            ],
            max_retries=4,
        )
        result = engine.run_with_self_healing(
            engine.build_state("task", tool_whitelist=["fetch_order"]), driver=driver
        )
        seen[driver] = (result.status, result.state.retry_count if result.state else -1)
    assert seen["while"] == seen["graph"] == (RunStatus.SELF_HEALED, 2)


# ---------------------------------------------------------------------------
# structural guarantees
# ---------------------------------------------------------------------------


def test_loop_context_exposes_only_five_fields() -> None:
    """A driver must not be able to reach the connector, the guards or the config."""
    annotations = LoopContext.__annotations__
    assert set(annotations) == {"engine", "state", "breaker", "tracer", "max_steps"}


def test_loop_engine_protocol_exposes_only_the_documented_capabilities() -> None:
    """A driver must reach the engine through a named, small surface.

    ``execute_tool_call`` was added for ``interrupt()``: with LangGraph the human's
    decision arrives *inside* the node, so the driver — not the engine — has to run
    the call the approval just authorised. It is still narrow: a driver can execute
    an already-authorised call, and cannot reach the guards, the config or the LLM.
    """
    from harness_core.loop import LoopEngine

    members = [name for name in dir(LoopEngine) if not name.startswith("_")]
    assert sorted(members) == [
        "after_attempt",
        "execute_tool_call",
        "step_execution_with_harness",
    ]


def test_engine_satisfies_the_loop_protocol() -> None:
    from harness_core.engine import HarnessOrchestrationEngine
    from harness_core.loop import LoopEngine

    for name in ("step_execution_with_harness", "after_attempt"):
        assert callable(getattr(HarnessOrchestrationEngine, name))
    # Runtime structural check against an instance.
    from tests.helpers import response_for as _r

    from harness_core.adapters.llm import FakeLLMAdapter
    from harness_core.adapters.mcp import FakeMCPConnector
    from harness_core.observability import StructuredLogger

    engine = HarnessOrchestrationEngine(
        llm=FakeLLMAdapter([_r()]),
        connector=FakeMCPConnector(),
        logger=StructuredLogger(enabled=False),
    )
    assert isinstance(engine, LoopEngine)


def test_run_accepts_a_driver_argument(engine_factory) -> None:
    engine, _ = engine_factory([response_for()])
    assert engine.run("task", tool_whitelist=["fetch_order"], driver="graph").ok
    assert engine.run("task", tool_whitelist=["fetch_order"], driver="while").ok
    with pytest.raises(ValueError, match="unknown loop driver"):
        engine.run("task", tool_whitelist=["fetch_order"], driver="nope")
