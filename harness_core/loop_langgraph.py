"""A real LangGraph driver — same decisions, different walker.

Why the state stays with the harness
------------------------------------
LangGraph merges each node's return value into its own state dict. That is exactly
the shape of the original bug this project was built to avoid: put ``history`` in
the graph state, and a node that returns a partial dict silently drops it.

So the graph state here carries only what the graph itself needs — the step counter
and the routing key — plus a reference to the :class:`LoopContext`. Every piece of
business state (``history``, ``error_stack``, ``retry_count``, ``status``) stays in
``ctx.state``, owned by the harness. LangGraph decides *where to go next*; it never
owns *what happened*.

That division is also what makes this driver swappable with the other two: all three
call the same ``ctx.attempt()`` and the same ``ctx.after()``.
"""

from __future__ import annotations

import time
from typing import Any, TypedDict

from .durable import SNAPSHOT_VERSION
from .errors import ERROR_TOOL_EXECUTION, ErrorStack, Finding
from .loop import Action, LoopContext, LoopOutcome
from .state import AgentHarnessState, RunStatus

#: Routing keys produced by the ``attempt`` node. ``exhausted`` is internal: it means
#: the step budget ran out, and routes to the breaker node.
_EXHAUSTED = "exhausted"


class _GraphState(TypedDict, total=False):
    """The *only* state LangGraph owns.

    Deliberately tiny. Business state lives in ``ctx.state`` and is never copied
    through the graph, so a node cannot accidentally truncate it.

    ``snapshot`` exists for the durable mode: a checkpointer serialises this dict
    with msgpack, and ``ctx`` holds live objects (engine, connector, sockets) that
    cannot be serialised. A durable walk therefore carries the serialisable snapshot
    and rebuilds ``ctx`` per process.
    """

    ctx: LoopContext
    snapshot: dict[str, Any]
    step: int
    action: str


def langgraph_available() -> bool:
    """Whether the optional ``langgraph`` extra is importable.

    Any failure counts as "not available". ``find_spec`` can raise (a broken
    install, a permission problem, a half-removed package) and letting that escape
    would make ``import harness_core`` fail on a machine that never asked for
    LangGraph — the exact opposite of an optional extra.
    """
    import importlib.util

    try:
        return importlib.util.find_spec("langgraph") is not None
    except (ImportError, ValueError, AttributeError, OSError):
        return False


class LangGraphLoopDriver:
    """Walks the self-healing loop with a compiled ``StateGraph``.

    The graph is::

        START -> attempt ─┬─ continue/retry ──> attempt
                          ├─ success/suspend/break ──> END
                          └─ exhausted ──> breaker ──> END

    Identical routing to :class:`~harness_core.loop.GraphLoopDriver`; the difference
    is that LangGraph performs the walk and enforces its own recursion limit.
    """

    name = "langgraph"

    def __init__(self, *, checkpointer: Any | None = None, interrupt_mode: bool = False) -> None:
        # When a checkpointer is supplied the graph is durable: the walk can be
        # resumed from a thread id in another process. Without one the driver is
        # exactly as before (in-memory, single process).
        self.checkpointer = checkpointer
        #: When True, a HITL suspension is handed to LangGraph's ``interrupt()``:
        #: the graph stops *inside* the node and the checkpoint records exactly where.
        #: Resuming then continues from that point instead of re-walking the loop.
        #:
        #: Requires a checkpointer — ``interrupt()`` without one raises, because
        #: there would be nowhere to remember the suspension.
        self.interrupt_mode = interrupt_mode
        if interrupt_mode and checkpointer is None:
            raise ValueError(
                "interrupt_mode needs a checkpointer: LangGraph's interrupt() has "
                "nowhere to record the suspension without one"
            )
        #: The context of the run currently being driven. Set by :meth:`drive` and
        #: read by the nodes when the graph state only carries a snapshot.
        self._ctx: LoopContext | None = None
        #: The last value passed to ``interrupt()``, exposed for callers that want to
        #: show it to a human without digging through the checkpoint.
        self.last_interrupt: dict[str, Any] | None = None

    @property
    def durable(self) -> bool:
        """Whether this driver persists across processes."""
        return self.checkpointer is not None

    # ------------------------------------------------------------------ graph
    def build(self) -> Any:
        """Compile the graph. Raises ``ImportError`` with a clear hint if absent."""
        try:
            from langgraph.graph import END, START, StateGraph
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise ImportError(
                "the 'langgraph' driver needs the optional extra: "
                "pip install 'harness-agent-core[langgraph]'"
            ) from exc

        graph: Any = StateGraph(_GraphState)
        graph.add_node("attempt", self._attempt_node)
        graph.add_node("breaker", self._breaker_node)
        graph.add_edge(START, "attempt")
        graph.add_conditional_edges(
            "attempt",
            self._route,
            {
                "attempt": "attempt",  # continue / retry: another model turn
                "breaker": "breaker",  # step budget exhausted
                END: END,  # success / suspend / break
            },
        )
        graph.add_edge("breaker", END)
        return graph.compile(checkpointer=self.checkpointer)

    # ------------------------------------------------------------------ nodes
    def _ctx_for(self, state: _GraphState) -> LoopContext:
        """Resolve the context for this step.

        In-memory mode carries it in the graph state. Durable mode cannot (msgpack
        rejects live objects), so it comes from the driver — which the caller has
        already rebuilt for this process.
        """
        ctx = state.get("ctx")
        if ctx is not None:
            return ctx
        if self._ctx is None:
            raise RuntimeError(
                "durable LangGraph walk has no context: call drive() with a "
                "LoopContext before invoking the graph"
            )
        return self._ctx

    def _attempt_node(self, state: _GraphState) -> _GraphState:
        ctx = self._ctx_for(state)
        step = int(state.get("step", 0))
        if step >= ctx.max_steps:
            return {"step": step, "action": _EXHAUSTED}

        # Resuming from ``interrupt()`` re-executes this node **from the top** — the
        # dict it was about to return was discarded when ``interrupt()`` raised, so no
        # graph-state marker can survive to say "we are resuming". The signal has to
        # come from the harness state, which *is* persisted.
        #
        # Why it matters: without this check the model turn runs a second time on
        # resume — an extra LLM call, and potentially a *different* pending tool call
        # than the one the human approved.
        resuming = self.interrupt_mode and _awaiting_decision(ctx.state)
        if not resuming:
            step += 1
            action: Action = ctx.after(ctx.attempt(step))
        else:
            action = "suspend"

        if action == "suspend" and self.interrupt_mode:
            # First pass: raises ``GraphInterrupt`` and parks the graph.
            # Resume pass: returns the human's answer and continues.
            action = self._interrupt_for_approval(ctx, step)

        updated: _GraphState = {"step": step, "action": action}
        if self.durable:
            # Keep the snapshot current so a crash mid-run resumes at this step.
            updated["snapshot"] = _snapshot_of_ctx(ctx, step=step, action=action)
        return updated

    def _interrupt_for_approval(self, ctx: LoopContext, step: int) -> Action:
        """Act on the human's answer after ``interrupt()`` resumes the node.

        What ``interrupt()`` buys over the default path
        -----------------------------------------------
        Without it the harness suspends by *returning* from the loop, and resuming
        means re-entering from the top. With it the graph stops inside the node and
        ``Command(resume=...)`` re-executes only that node — so the model turns that
        already ran are not repeated.

        The cost of that, which this method exists to pay: the node re-runs **from the
        top**, so the model turn must be skipped on the resume pass. ``_attempt_node``
        decides that from the persisted harness state (``_awaiting_decision``), and
        this method is then the only thing the resumed node does besides ``interrupt``.

        The answer is interpreted with the same rules as the gateway: anything that is
        not an explicit approval is a rejection. A human who says "maybe" must not
        authorise a destructive tool.
        """
        from langgraph.types import interrupt

        call = ctx.state.pending_tool_call
        payload = _interrupt_payload(ctx, step)
        self.last_interrupt = payload
        # First pass: raises GraphInterrupt, parking the graph here.
        # Resume pass: returns whatever the caller passed to Command(resume=...).
        answer = interrupt(payload)
        approved = _is_approval(answer)
        ctx.state.approval_decision = "approve" if approved else "reject"
        ctx.state.approval_reason = _answer_reason(answer)
        ctx.state.metadata["approval_answer"] = answer

        if not approved:
            ctx.state.status = RunStatus.REJECTED_BY_HUMAN
            ctx.state.failure_reason = (
                f"human rejected {call.tool_name!r}: {ctx.state.approval_reason}"
                if call is not None
                else ctx.state.approval_reason
            )
            return "break"

        # The tool did *not* run when the engine suspended — approval is what
        # authorises execution. So it runs here, exactly once. Re-running it later
        # (or before) would execute a side-effecting tool twice.
        if call is None:
            ctx.state.status = RunStatus.RUNNING
            return "continue"
        result = ctx.engine.execute_tool_call(ctx.state, call)
        ctx.state.metadata["last_tool_result"] = result.to_dict()
        if not result.ok:
            ctx.state.fail(
                ErrorStack(
                    step=f"tool.{call.tool_name}",
                    findings=[
                        Finding(
                            failed_gate=ERROR_TOOL_EXECUTION,
                            message=result.error or "approved tool failed",
                            tool_name=call.tool_name,
                            resolution_hint=f"已批准的工具 {call.tool_name} 执行失败",
                        )
                    ],
                    trace=(result.meta or {}).get("traceback"),
                )
            )
            return "retry"
        ctx.state.execution_result = result.content
        ctx.state.pending_tool_call = None
        ctx.state.error_stack = None
        ctx.state.status = RunStatus.RUNNING
        # The tool ran; the model gets another turn to produce the deliverable.
        return "continue"

    def _breaker_node(self, state: _GraphState) -> _GraphState:
        ctx = self._ctx_for(state)
        ctx.state.status = RunStatus.FAILED_WITH_CIRCUIT_BREAKER
        ctx.state.failure_reason = (
            f"max_steps={ctx.max_steps} exhausted without a validated deliverable"
        )
        updated: _GraphState = {"step": int(state.get("step", 0)), "action": "break"}
        if self.durable:
            updated["snapshot"] = _snapshot_of_ctx(
                ctx, step=int(state.get("step", 0)), action="break"
            )
        return updated

    @staticmethod
    def _route(state: _GraphState) -> str:
        from langgraph.graph import END

        action = state.get("action", "break")
        if action in ("continue", "retry"):
            return "attempt"
        if action == _EXHAUSTED:
            return "breaker"
        return END

    # ------------------------------------------------------------------ drive
    def drive(self, ctx: LoopContext) -> LoopOutcome:
        import time

        started = time.perf_counter()
        self._ctx = ctx
        app = self.build()
        # LangGraph counts every node execution against ``recursion_limit``. Its
        # default is generous (10007 in langgraph 1.x), but it is still a hard stop:
        # a very large step budget would hit it and surface as a GraphRecursionError
        # instead of a clean terminal status. Scaling it keeps the harness's own
        # ``max_steps`` the only budget that decides when a run stops.
        config: dict[str, Any] = {"recursion_limit": ctx.max_steps * 3 + 10}
        initial: _GraphState = {"ctx": ctx, "step": 0, "action": "start"}
        if self.durable:
            # A durable graph must not carry live objects: msgpack would reject the
            # whole checkpoint and the run would fail on the first step.
            config["configurable"] = {"thread_id": ctx.state.trace_id}
            initial = {
                "snapshot": _snapshot_of_ctx(ctx, step=0, action="start"),
                "step": 0,
                "action": "start",
            }
        final = app.invoke(initial, config=config)
        return LoopOutcome(
            status=ctx.state.status,
            steps=int(final.get("step", 0)),
            started_at=started,
        )

    # ---------------------------------------------------------------- durable
    def checkpoint_id(self, ctx: LoopContext) -> dict[str, Any]:
        """The ``config`` that identifies this run in the checkpointer."""
        return {"configurable": {"thread_id": ctx.state.trace_id}}

    def save_snapshot(self, ctx: LoopContext, *, step: int, action: str = "start") -> dict[str, Any]:
        """Write the current state into the checkpointer under this run's thread id."""
        if self.checkpointer is None:
            raise RuntimeError("save_snapshot() needs a checkpointer")
        payload = _snapshot_of_ctx(ctx, step=step, action=action)
        # ``update_state`` is how LangGraph stores a checkpoint without executing a
        # node; writing through it keeps the resume path identical to a live walk.
        app = self.build()
        app.update_state(self.checkpoint_id(ctx), {"snapshot": payload, "step": step})
        return payload

    def load_snapshot(self, thread_id: str) -> dict[str, Any] | None:
        """Read the last persisted snapshot for ``thread_id``, if any."""
        if self.checkpointer is None:
            raise RuntimeError("load_snapshot() needs a checkpointer")
        app = self.build()
        config = {"configurable": {"thread_id": thread_id}}
        state = app.get_state(config)
        values = getattr(state, "values", None) or {}
        snapshot = values.get("snapshot")
        return dict(snapshot) if isinstance(snapshot, dict) else None

    # --------------------------------------------------------------- interrupt
    def pending_interrupt(self, thread_id: str) -> dict[str, Any] | None:
        """The payload this run is suspended on, or ``None`` if it is not suspended.

        Reading this is how a UI knows there is something to ask about: an empty
        result means the run finished (or never started), not "approval pending".
        """
        if self.checkpointer is None:
            raise RuntimeError("pending_interrupt() needs a checkpointer")
        app = self.build()
        config = {"configurable": {"thread_id": thread_id}}
        state = app.get_state(config)
        interrupts = getattr(state, "interrupts", None) or ()
        for item in interrupts:
            value = getattr(item, "value", None)
            if isinstance(value, dict):
                return dict(value)
        return None

    def is_suspended(self, thread_id: str) -> bool:
        """Whether the graph stopped at an ``interrupt()`` and is waiting for input."""
        if self.checkpointer is None:
            return False
        app = self.build()
        config = {"configurable": {"thread_id": thread_id}}
        state = app.get_state(config)
        return bool(getattr(state, "interrupts", None) or ())

    def resume(
        self,
        ctx: LoopContext,
        answer: Any,
        *,
        thread_id: str | None = None,
    ) -> LoopOutcome:
        """Continue a suspended graph from the ``interrupt()`` point.

        Unlike ``engine.resume()``, this does **not** re-walk the loop: LangGraph
        restores the checkpoint and re-executes only the interrupted node, with
        ``interrupt()`` returning ``answer`` instead of raising.

        The context must be the same run (same ``state``); the engine and connector
        are supplied by the caller because a checkpoint cannot hold live objects.
        """
        if not self.interrupt_mode:
            raise RuntimeError(
                "resume() needs interrupt_mode=True: without it there is no "
                "suspension point to resume from"
            )
        from langgraph.types import Command

        started = time.perf_counter()
        self._ctx = ctx
        app = self.build()
        config: dict[str, Any] = {
            "recursion_limit": ctx.max_steps * 3 + 10,
            "configurable": {"thread_id": thread_id or ctx.state.trace_id},
        }
        final = app.invoke(Command(resume=answer), config=config)
        return LoopOutcome(
            status=ctx.state.status,
            steps=int(final.get("step", 0)),
            started_at=started,
        )


def _snapshot_of_ctx(ctx: LoopContext, *, step: int, action: str) -> dict[str, Any]:
    """Serialise the run: business state only, never the machinery."""
    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "step": step,
        "action": action,
        "state": ctx.state.to_dict(),
    }


#: Answers that count as approval. Anything else is a rejection — a human who says
#: "maybe", "later" or "hmm" must not authorise a destructive tool.
_APPROVAL_WORDS = frozenset({"approve", "approved", "yes", "y", "ok", "allow", "true"})


def _interrupt_payload(ctx: LoopContext, step: int) -> dict[str, Any]:
    """What to show the human while the graph is parked.

    Deliberately plain data: it is serialised into the checkpoint, so it must not
    hold a ``ToolCall`` or anything else with behaviour.
    """
    call = ctx.state.pending_tool_call
    return {
        "request_id": ctx.state.metadata.get("approval_request_id"),
        "trace_id": ctx.state.trace_id,
        "tool_name": call.tool_name if call is not None else None,
        "arguments": dict(call.arguments) if call is not None else {},
        "task_prompt": ctx.state.task_prompt,
        "reason": "high-risk tool requires human approval",
        "step": step,
    }


def _awaiting_decision(state: AgentHarnessState) -> bool:
    """Whether this run is parked on an approval that has not been answered yet.

    Read from the harness state rather than the graph state because the graph-state
    marker is lost when ``interrupt()`` raises (the node's return value is discarded).

    ``approval_decision is None`` is the load-bearing part: the engine sets the
    decision only when a gateway answered, so a ``PENDING_HUMAN_APPROVAL`` run with no
    decision is one that is genuinely waiting. Checking the status alone would also
    match a run parked by a gateway that returned ``timeout`` — and resuming on a
    timeout would silently turn "nobody answered" into a decision.
    """
    return (
        state.status is RunStatus.PENDING_HUMAN_APPROVAL
        and state.approval_decision is None
        and state.pending_tool_call is not None
    )


def _is_approval(answer: Any) -> bool:
    """Whether a human's answer authorises execution.

    Structured answers are accepted too (``{"decision": "approve"}``), because a UI
    will usually send an object rather than a bare string. Everything unrecognised is
    treated as a rejection: the failure mode of "denied a legitimate call" is
    recoverable, the failure mode of "ran a destructive call nobody approved" is not.
    """
    if isinstance(answer, bool):
        return answer
    if isinstance(answer, dict):
        for key in ("decision", "approved", "answer", "action"):
            if key in answer:
                return _is_approval(answer[key])
        return False
    if isinstance(answer, str):
        return answer.strip().lower() in _APPROVAL_WORDS
    return False


def _answer_reason(answer: Any) -> str:
    """A human-readable reason, preserving the human's own words when there are any."""
    if isinstance(answer, dict):
        for key in ("reason", "comment", "note", "message"):
            value = answer.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return "rejected by human"
    if isinstance(answer, str) and answer.strip():
        return answer.strip()
    return "rejected by human"
