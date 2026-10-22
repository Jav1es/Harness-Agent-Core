"""Pluggable control flow for the self-healing loop.

Why this module exists
----------------------
The engine's *decisions* (should we retry? is the breaker open? did a tool run?)
must not be entangled with *how the loop is walked*. Keeping them apart is what
makes "swap the while-loop for LangGraph" a real statement instead of a README
claim.

Two drivers ship here, and the test suite asserts they produce identical
outcomes:

* :class:`WhileLoopDriver` — the default, an explicit bounded ``while``.
* :class:`GraphLoopDriver` — an explicit node/edge graph walked by a router. It
  mirrors the LangGraph execution model (nodes return a routing key, edges decide
  the next node) **without depending on LangGraph**, so the package keeps its zero
  dependency budget.

The decision itself lives in one place — ``engine.after_attempt`` — so the two
drivers cannot drift.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from .adapters.mcp import ToolResult
from .circuit_breaker import CircuitBreaker
from .observability import Tracer
from .state import AgentHarnessState, RunStatus, ToolCall

#: What should happen after one attempt.
Action = Literal["success", "continue", "suspend", "retry", "break"]


@runtime_checkable
class LoopEngine(Protocol):
    """The two engine capabilities a driver needs — nothing more.

    Declaring it as a protocol (instead of ``Any``) keeps the drivers type-checked
    and documents the real coupling: a driver may execute attempts and ask for a
    routing decision, and it cannot reach into the guards, the config or the
    connector.
    """

    def step_execution_with_harness(
        self, state: AgentHarnessState, *, attempt: int, prompt: str | None = None
    ) -> RunStatus: ...

    def after_attempt(
        self,
        state: AgentHarnessState,
        status: RunStatus,
        breaker: CircuitBreaker,
        tracer: Tracer,
    ) -> Action: ...

    def execute_tool_call(self, state: AgentHarnessState, call: ToolCall) -> ToolResult:
        """Run an already-authorised tool call.

        Needed by drivers that own the approval point: with LangGraph's
        ``interrupt()`` the decision arrives *inside* the node, so the driver — not
        the engine — has to perform the execution the approval just authorised.
        """
        ...


@dataclass
class LoopOutcome:
    """Result of walking the loop: where it stopped and how many steps it took."""

    status: RunStatus
    steps: int
    started_at: float = field(default_factory=time.perf_counter)


@dataclass
class LoopContext:
    """Everything a driver is allowed to touch.

    Passing a context instead of the engine keeps the drivers testable in isolation
    and makes the coupling explicit.
    """

    engine: LoopEngine
    state: AgentHarnessState
    breaker: CircuitBreaker
    tracer: Tracer
    max_steps: int

    def attempt(self, step: int) -> RunStatus:
        return self.engine.step_execution_with_harness(self.state, attempt=step)

    def after(self, status: RunStatus) -> Action:
        return self.engine.after_attempt(self.state, status, self.breaker, self.tracer)


class LoopDriver(Protocol):
    """Anything that can walk the attempt loop to a terminal status."""

    name: str

    def drive(self, ctx: LoopContext) -> LoopOutcome: ...


class WhileLoopDriver:
    """The default driver: a bounded ``while`` with an explicit exit for every case."""

    name = "while"

    def drive(self, ctx: LoopContext) -> LoopOutcome:
        started = time.perf_counter()
        step = 0
        while step < ctx.max_steps:
            step += 1
            action = ctx.after(ctx.attempt(step))
            if action in ("success", "suspend", "break"):
                return LoopOutcome(status=ctx.state.status, steps=step, started_at=started)
            # "continue": a tool ran, the model gets another turn.
            # "retry": the attempt was rejected; the breaker already accounted for it.
        # Step budget exhausted without a terminal verdict.
        ctx.state.status = RunStatus.FAILED_WITH_CIRCUIT_BREAKER
        ctx.state.failure_reason = (
            f"max_steps={ctx.max_steps} exhausted without a validated deliverable"
        )
        return LoopOutcome(
            status=RunStatus.FAILED_WITH_CIRCUIT_BREAKER, steps=step, started_at=started
        )


class GraphLoopDriver:
    """A node/edge graph walker with LangGraph-shaped semantics.

    The shape is deliberately the same as a LangGraph state machine::

        START -> attempt -> route -> {finish | suspend | retry | breaker}
        retry -> attempt
        breaker -> finish

    A node receives the context and returns a routing key; ``EDGES`` maps keys to
    the next node. Swapping this class for a real LangGraph ``StateGraph`` means
    replacing ``drive`` — the engine, the guards, the breaker and the tracer stay
    untouched.
    """

    name = "graph"

    #: Routing key -> next node. ``None`` terminates the walk.
    EDGES: dict[str, str | None] = {
        "continue": "attempt",
        "retry": "attempt",
        "success": None,
        "suspend": None,
        "break": None,
    }

    def drive(self, ctx: LoopContext) -> LoopOutcome:
        started = time.perf_counter()
        node: str | None = "attempt"
        step = 0

        while node is not None:
            if node == "attempt":
                if step >= ctx.max_steps:
                    node = "breaker"
                    continue
                step += 1
                key = ctx.after(ctx.attempt(step))
                node = self.EDGES[key]
            elif node == "breaker":
                ctx.state.status = RunStatus.FAILED_WITH_CIRCUIT_BREAKER
                ctx.state.failure_reason = (
                    f"max_steps={ctx.max_steps} exhausted without a validated deliverable"
                )
                node = None
            else:  # pragma: no cover - guards against a typo in EDGES
                raise RuntimeError(f"unknown graph node {node!r}")

        return LoopOutcome(status=ctx.state.status, steps=step, started_at=started)


def _register_optional_drivers(registry: dict[str, LoopDriver]) -> None:
    """Add the LangGraph driver when the optional extra is installed.

    Registration is import-guarded so the package keeps its zero-dependency runtime:
    ``harness-core run --driver langgraph`` works when langgraph is present, and
    fails with a clear message when it is not, instead of breaking
    ``import harness_core`` for everyone.
    """
    from .loop_langgraph import LangGraphLoopDriver, langgraph_available

    if langgraph_available():
        registry["langgraph"] = LangGraphLoopDriver()


DRIVERS: dict[str, LoopDriver] = {
    "while": WhileLoopDriver(),
    "graph": GraphLoopDriver(),
}
_register_optional_drivers(DRIVERS)


def get_driver(name: str | LoopDriver) -> LoopDriver:
    """Resolve a driver by name, or accept one directly.

    Accepting an instance matters for durable drivers: a checkpointer-configured
    ``LangGraphLoopDriver`` cannot be described by a bare name, because the name
    lookup would return the registry's unconfigured instance. Passing ``"langgraph"``
    would silently drop the checkpointer — a resume that looks wired but is not.
    """
    if not isinstance(name, str):
        if not hasattr(name, "drive"):
            raise ValueError(
                f"{type(name).__name__} is not a loop driver (it has no drive())"
            )
        return name
    try:
        return DRIVERS[name]
    except KeyError as exc:
        raise ValueError(
            f"unknown loop driver {name!r}; available: {sorted(DRIVERS)}"
        ) from exc
