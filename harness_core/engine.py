"""The orchestration engine: the deterministic layer between model and business system.

The public surface mirrors the product spec exactly:

==============================  =====================================================
``guard_tool_call``             whitelist + argument-schema + risk gate for one call
``deterministic_validator``     ``(ok, error_message)`` over the deliverable
``step_execution_with_harness`` one attempt: plan -> guard -> execute -> validate
``run_with_self_healing``       the bounded loop, with breaker and HITL
==============================  =====================================================

Two counters, deliberately separate:

* ``step`` — how many model turns we have taken. Bounded by ``max_steps``. A turn
  that successfully calls a tool is *progress*, not a failure, so it must not burn
  the repair budget.
* ``retry_count`` — how many attempts were rejected. Bounded by ``max_retries`` and
  watched by the circuit breaker.
"""

from __future__ import annotations

import json
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .adapters.llm import LLMAdapter, LLMResponse
from .adapters.mcp import MCPConnector, ToolDefinition, ToolResult
from .circuit_breaker import BreakerDecision, CircuitBreaker
from .errors import (
    ERROR_LLM,
    ERROR_OUTPUT_RULE,
    ERROR_OUTPUT_SCHEMA,
    ERROR_TOOL_EXECUTION,
    ErrorStack,
    HarnessInfrastructureError,
    Finding,
)
from .guards import OutputGuard, ToolGuard, ToolSpec, validate_json_schema
from .hitl import ApprovalResponse, HumanApprovalGateway
from .loop import Action, LoopContext, get_driver
from .observability import StructuredLogger, Tracer, estimate_payload_tokens
from .redact import OutputRedactor
from .semantic import SemanticLayer
from .state import AgentHarnessState, HistoryEntry, RunStatus, ToolCall
from .untrusted import scan_payload, signals_to_findings, wrap_untrusted

#: A pluggable validator: return ``True`` / ``None`` to pass, ``False`` or a string
#: to fail (the string becomes the repair hint).
ValidatorFn = Callable[[Any], bool | str | None]


@dataclass
class HarnessRunResult:
    """What ``run`` / ``resume`` hand back to the caller."""

    trace_id: str
    status: RunStatus
    attempts: int = 0
    execution_result: Any = None
    error_stack: ErrorStack | None = None
    warnings: list[Finding] = field(default_factory=list)
    failure_reason: str | None = None
    approval_request_id: str | None = None
    state: AgentHarnessState | None = None
    trace: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    tokens_estimate: int = 0

    @property
    def ok(self) -> bool:
        return self.status.is_success

    @property
    def exit_code(self) -> int:
        """0 for success, 1 for any non-success (used by the CLI / CI gate)."""
        return 0 if self.ok else 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "status": self.status.value,
            "ok": self.ok,
            "attempts": self.attempts,
            "execution_result": self.execution_result,
            "error_stack": self.error_stack.to_dict() if self.error_stack else None,
            "warnings": [w.to_dict() for w in self.warnings],
            "failure_reason": self.failure_reason,
            "approval_request_id": self.approval_request_id,
            "duration_ms": round(self.duration_ms, 3),
            "tokens_estimate": self.tokens_estimate,
            "history": [entry.to_dict() for entry in (self.state.history if self.state else [])],
        }


class HarnessOrchestrationEngine:
    """Drives one task from prompt to a deterministic, auditable outcome."""

    def __init__(
        self,
        llm: LLMAdapter,
        connector: MCPConnector,
        *,
        output_guard: OutputGuard | None = None,
        approval_gateway: HumanApprovalGateway | None = None,
        tracer: Tracer | None = None,
        logger: StructuredLogger | None = None,
        breaker: CircuitBreaker | None = None,
        tool_specs: dict[str, ToolSpec] | None = None,
        high_risk_tools: set[str] | None = None,
        max_retries: int = 3,
        max_steps: int = 8,
        validators: dict[str, ValidatorFn] | None = None,
        output_schema: dict[str, Any] | None = None,
        semantic_layer: SemanticLayer | None = None,
        redactor: OutputRedactor | None = None,
    ) -> None:
        self.llm = llm
        self.connector = connector
        self.output_guard = output_guard or OutputGuard(output_schema=output_schema)
        if output_schema is not None:
            self.output_guard.output_schema = output_schema
        self.approval_gateway = approval_gateway
        self.tracer = tracer
        self.logger = logger or StructuredLogger()
        self.breaker = breaker
        self.tool_specs: dict[str, ToolSpec] = dict(tool_specs or {})
        self.high_risk_tools: set[str] = set(high_risk_tools or set())
        self.max_retries = max_retries
        self.max_steps = max_steps
        self.validators: dict[str, tuple[ValidatorFn, str]] = {
            name: (fn, "BLOCK") for name, fn in (validators or {}).items()
        }
        # The semantic layer is a *separate* layer, not another validator. Its
        # findings carry their own gate prefix so a reader can always tell a
        # judgement from a deterministic fact.
        self.semantic_layer = semantic_layer
        #: Output-side redaction. ``None`` disables it entirely, which is the
        #: default: silently rewriting a caller's deliverable would be surprising.
        self.redactor = redactor

    # ==================================================================
    # 1. Tool orchestration
    # ==================================================================
    def guard_tool_call(self, state: AgentHarnessState, call: ToolCall) -> list[Finding]:
        """Authorise one requested tool call. Empty list == may execute.

        This is the *only* place that decides whether a tool runs. It is a pure
        check: it never touches the connector, so a denied call costs nothing and
        cannot leak side effects. It annotates ``call.decision`` for the trace.
        """
        guard = self._tool_guard(state)
        findings = guard.check(call.tool_name, call.arguments)

        if findings:
            call.decision = "deny"
            call.reason = findings[0].hint
            self._log(
                "tool.denied",
                trace_id=state.trace_id,
                tool=call.tool_name,
                gate=findings[0].failed_gate,
            )
            return findings

        if guard.is_high_risk(call.tool_name):
            call.decision = "require_approval"
            call.reason = "high-risk tool requires human approval"
        else:
            call.decision = "allow"
        self._log(
            "tool.allowed",
            trace_id=state.trace_id,
            tool=call.tool_name,
            decision=call.decision,
        )
        return []

    def _tool_guard(self, state: AgentHarnessState) -> ToolGuard:
        specs: dict[str, ToolSpec] = {}
        for definition in self._discover_tools():
            existing = self.tool_specs.get(definition.name)
            specs[definition.name] = existing or ToolSpec(
                name=definition.name,
                description=definition.description,
                input_schema=definition.input_schema,
                high_risk=definition.name in self.high_risk_tools,
            )
        for name, spec in self.tool_specs.items():
            specs.setdefault(name, spec)
        return ToolGuard(
            whitelist=state.tool_whitelist,
            specs=specs,
            high_risk_tools=state.high_risk_tools or self.high_risk_tools,
        )

    def _discover_tools(self) -> list[ToolDefinition]:
        try:
            return self.connector.list_tools()
        except HarnessInfrastructureError:
            # Discovery failing means the transport is dead. Returning [] here would
            # make the run look healthy: the whitelist becomes empty, the model is
            # offered no tools, and a scripted planner can still emit a "valid"
            # deliverable — a green run against a server that never started.
            raise
        except Exception as exc:  # noqa: BLE001 - a broken connector is a run failure
            self._log("connector.list_tools_failed", error=str(exc))
            return []

    def execute_tool_call(self, state: AgentHarnessState, call: ToolCall) -> ToolResult:
        """Run an already-authorised tool call and record a tool span."""
        tracer = self._tracer(state)
        span = tracer.span("tool.call", tool=call.tool_name)
        started = time.perf_counter()
        try:
            result = self.connector.call_tool(call.tool_name, call.arguments)
        except HarnessInfrastructureError:
            # Same reasoning as the LLM boundary: a broken fixture must not be
            # laundered into a tool failure that the model is asked to "fix".
            tracer.end_span(span, "infrastructure_error", tool=call.tool_name)
            raise
        except Exception as exc:  # noqa: BLE001 - connector boundary
            result = ToolResult(
                tool_name=call.tool_name,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                meta={"traceback": traceback.format_exc(limit=3)},
            )
        elapsed = (time.perf_counter() - started) * 1000.0
        tracer.end_span(
            span,
            "ok" if result.ok else "error",
            tokens_estimate=estimate_payload_tokens(result.content),
            tool=call.tool_name,
        )
        self._log(
            "tool.executed",
            trace_id=state.trace_id,
            tool=call.tool_name,
            ok=result.ok,
            duration_ms=round(elapsed, 3),
        )
        return result

    # ==================================================================
    # 2. Deterministic validation
    # ==================================================================
    def deterministic_validator(
        self, payload: Any, schema: dict[str, Any] | None = None
    ) -> tuple[bool, str]:
        """Pure, offline validation of the deliverable.

        Returns ``(True, "")`` or ``(False, error_message)``. The message is the
        exact block that gets fed back into the next prompt, so it is written to be
        actionable rather than pretty.

        Pluggable: :meth:`register_validator` adds Python predicates; the output
        guard adds schema / forbidden content / business rules.
        """
        findings = self.validate_output(payload, schema)
        blocking = [f for f in findings if f.severity == "BLOCK"]
        if not blocking:
            return True, ""
        return False, ErrorStack(step="validate", findings=blocking).to_prompt_block()

    def validate_output(
        self, payload: Any, schema: dict[str, Any] | None = None
    ) -> list[Finding]:
        """Structured form of :meth:`deterministic_validator`."""
        if payload is None:
            return [
                Finding(
                    failed_gate=ERROR_OUTPUT_SCHEMA,
                    message="the model returned no deliverable",
                    resolution_hint="必须返回结构化的最终结果（JSON 对象）",
                )
            ]
        findings: list[Finding] = []
        if schema is not None:
            findings.extend(
                validate_json_schema(payload, schema, gate_prefix=ERROR_OUTPUT_SCHEMA)
            )
        findings.extend(self.output_guard.check(payload))

        for name, (fn, severity) in self.validators.items():
            try:
                outcome = fn(payload)
            except Exception as exc:  # noqa: BLE001 - a broken rule must not pass silently
                findings.append(
                    Finding(
                        failed_gate=f"{ERROR_OUTPUT_RULE}.{name}.eval_error",
                        message=f"validator {name} raised {type(exc).__name__}: {exc}",
                        resolution_hint=f"校验器 {name} 自身抛异常，先修校验器",
                    )
                )
                continue
            if outcome is True or outcome is None:
                continue
            hint = outcome if isinstance(outcome, str) else f"custom validator {name} failed"
            findings.append(
                Finding(
                    failed_gate=f"{ERROR_OUTPUT_RULE}.{name}",
                    message=hint,
                    severity=severity,  # type: ignore[arg-type]
                    resolution_hint=hint,
                )
            )
        return findings

    def register_validator(self, name: str, fn: ValidatorFn, *, severity: str = "BLOCK") -> None:
        """Add a pluggable deterministic check."""
        self.validators[name] = (fn, severity)

    # ==================================================================
    # 3. One attempt
    # ==================================================================
    def step_execution_with_harness(
        self,
        state: AgentHarnessState,
        *,
        attempt: int,
        prompt: str | None = None,
    ) -> RunStatus:
        """Run exactly one attempt and update ``state`` in place.

        Returns ``RUNNING`` when a tool executed and another model turn is needed,
        a success status when the deliverable passed, or a failure/pending status.
        Expected failures never raise: they become a structured ``error_stack``.
        """
        tracer = self._tracer(state)
        state.status = RunStatus.RUNNING
        span = tracer.span("attempt", attempt=attempt)
        started = time.perf_counter()
        try:
            # The body mutates ``state.status``; the return value is the same status
            # and is kept for callers that prefer an explicit value.
            self._attempt_body(state, attempt=attempt, prompt=prompt)
        finally:
            elapsed = (time.perf_counter() - started) * 1000.0
            tracer.end_span(
                span,
                state.status.value,
                tokens_estimate=int(state.metadata.get("last_tokens", 0)),
                attempt=attempt,
            )

        state.record(
            HistoryEntry(
                attempt=attempt,
                phase="step_execution_with_harness",
                status=state.status.value,
                tool_name=(state.pending_tool_call.tool_name if state.pending_tool_call else None),
                error_summary=(state.error_stack.to_prompt_block() if state.error_stack else None),
                duration_ms=elapsed,
                tokens_estimate=int(state.metadata.get("last_tokens", 0)),
            )
        )
        return state.status

    def _attempt_body(
        self, state: AgentHarnessState, *, attempt: int, prompt: str | None
    ) -> RunStatus:
        tracer = self._tracer(state)
        rendered = prompt if prompt is not None else self.build_prompt(state, attempt=attempt)

        # ---- 1. ask the model ------------------------------------------------
        llm_span = tracer.span("llm.generate", attempt=attempt)
        try:
            response: LLMResponse = self.llm.generate(
                rendered, state, tools=[d.to_dict() for d in self._discover_tools()]
            )
        except HarnessInfrastructureError:
            # A broken fixture (e.g. an exhausted cassette) is not a task failure.
            # Retrying cannot fix it, and dressing it up as a BLOCK finding would
            # make a broken test harness look like a plausible model failure.
            tracer.end_span(llm_span, "infrastructure_error")
            raise
        except Exception as exc:  # noqa: BLE001 - adapter boundary
            tracer.end_span(llm_span, "error")
            state.fail(
                ErrorStack(
                    step="llm.generate",
                    findings=[
                        Finding(
                            failed_gate=ERROR_LLM,
                            message=f"{type(exc).__name__}: {exc}",
                            resolution_hint="模型适配器调用失败，检查配置后重试",
                        )
                    ],
                    trace=traceback.format_exc(limit=5),
                )
            )
            return state.status

        tokens = response.tokens_estimate or estimate_payload_tokens(
            response.content if response.content is not None else response.raw_text
        )
        state.metadata["last_tokens"] = tokens
        tracer.end_span(llm_span, "ok" if not response.error else "error", tokens_estimate=tokens)
        state.current_plan = {
            "attempt": attempt,
            "tool_call": response.tool_call.to_dict() if response.tool_call else None,
            "has_content": response.content is not None,
            "model": response.model,
        }

        if response.error:
            state.fail(
                ErrorStack(
                    step="llm.generate",
                    findings=[
                        Finding(
                            failed_gate=ERROR_LLM,
                            message=response.error,
                            resolution_hint="模型未返回可用响应，检查适配器脚本或服务状态",
                        )
                    ],
                )
            )
            return state.status

        # ---- 2. tool request? ------------------------------------------------
        if response.tool_call is not None:
            call = ToolCall(
                tool_name=response.tool_call.tool_name,
                arguments=dict(response.tool_call.arguments),
            )
            state.pending_tool_call = call

            denied = self.guard_tool_call(state, call)
            if denied:
                state.fail(ErrorStack(step="guard_tool_call", findings=denied))
                return state.status

            # ---- 3. human approval for high-risk tools ----------------------
            if call.decision == "require_approval":
                return self._suspend_for_approval(state, call)

            # ---- 4. execute --------------------------------------------------
            result = self.execute_tool_call(state, call)
            state.metadata["last_tool_result"] = result.to_dict()
            if not result.ok:
                state.fail(
                    ErrorStack(
                        step=f"tool.{call.tool_name}",
                        findings=[
                            Finding(
                                failed_gate=ERROR_TOOL_EXECUTION,
                                message=result.error or "tool failed without a message",
                                tool_name=call.tool_name,
                                resolution_hint=(
                                    f"工具 {call.tool_name} 执行失败：{result.error}；"
                                    "检查参数或换一个可用工具"
                                ),
                            )
                        ],
                        trace=(result.meta or {}).get("traceback"),
                    )
                )
                return state.status

            # A successful tool call is progress: the next model turn sees the
            # result and produces the deliverable. It does not burn the retry budget.
            state.execution_result = result.content
            state.pending_tool_call = None
            state.error_stack = None
            state.metadata["last_phase"] = f"tool.{call.tool_name}"

            # Tool output is untrusted input. Scan it so the event is visible in the
            # trace, and record the signals as WARN — never BLOCK. A substring match
            # is evidence of suspicious text, not proof of compromise; the
            # load-bearing defence is the tool whitelist, which runs before this.
            signals = scan_payload(result.content)
            if signals:
                state.metadata["untrusted_signals"] = [s.to_dict() for s in signals]
                state.warnings = [
                    *state.warnings,
                    *signals_to_findings(signals, source=f"tool {call.tool_name}"),
                ]
                self._log(
                    "untrusted.content_detected",
                    tool=call.tool_name,
                    signals=[s.name for s in signals],
                )
            state.status = RunStatus.RUNNING
            return state.status

        # ---- 4b. redact secrets BEFORE anything else looks at the payload ----
        # Order matters: if a card number is still present when the forbidden-pattern
        # gate runs, the run blocks on a string that was about to be removed anyway.
        # Redact first, then validate what is actually leaving the harness.
        payload = response.content
        redaction_findings: list[Finding] = []
        if self.redactor is not None and payload is not None:
            redact_span = tracer.span("redact", attempt=attempt)
            payload, redaction_findings = self.redactor.redact(payload)
            tracer.end_span(
                redact_span,
                "ok" if not redaction_findings else "redacted",
                redacted=sum(1 for _ in redaction_findings),
            )
            if redaction_findings:
                state.metadata["redactions"] = [f.failed_gate for f in redaction_findings]
            # Note: the findings are *not* appended to state.warnings here. They are
            # already part of `findings` below, which is what the success path copies
            # into warnings. Appending in both places listed every redaction twice.

        # ---- 5. deterministic validation of the deliverable -----------------
        validate_span = tracer.span("validate", attempt=attempt)
        findings = [*redaction_findings, *self.validate_output(payload)]
        blocking = [f for f in findings if f.severity == "BLOCK"]
        tracer.end_span(
            validate_span,
            "ok" if not blocking else "error",
            blocking=len(blocking),
            warnings=len(findings) - len(blocking),
        )

        if blocking:
            state.fail(ErrorStack(step="validate", findings=findings))
            return state.status

        # ---- 5b. semantic layer (costs money, so only on a clean payload) ----
        # Deliberately after the deterministic gates: there is no point paying a
        # judge to review a payload that already failed a schema check. The cost of
        # a judgement is real, and a failed run should be as cheap as possible.
        if self.semantic_layer is not None and self.semantic_layer.requirements:
            semantic_span = tracer.span("semantic", attempt=attempt)
            # Judge the redacted payload: a judge that sees the secret defeats the
            # point of redacting it, and its findings would quote it back.
            semantic_findings = self.semantic_layer.check(payload)
            semantic_blocking = [f for f in semantic_findings if f.severity == "BLOCK"]
            tracer.end_span(
                semantic_span,
                "ok" if not semantic_blocking else "error",
                checks_run=self.semantic_layer.checks_run,
                tokens_spent=self.semantic_layer.tokens_spent,
                blocking=len(semantic_blocking),
            )
            state.metadata["semantic"] = self.semantic_layer.to_dict()
            if semantic_findings:
                findings = [*findings, *semantic_findings]
            if semantic_blocking:
                state.fail(ErrorStack(step="semantic", findings=findings))
                return state.status

        # ---- 6. accepted -----------------------------------------------------
        # Preserve warnings accumulated earlier in the run (e.g. injection signals
        # found in tool output). Assigning instead of extending would silently drop
        # them exactly when the run succeeds — the worst moment to lose a security
        # signal, because nothing else in the report would mention it.
        state.warnings = [
            *state.warnings,
            *(f for f in findings if f.severity != "BLOCK"),
        ]
        # The *redacted* payload is the deliverable. Storing the original here would
        # undo the whole exercise: every consumer downstream — the caller, the trace,
        # the resume snapshot — would receive the secret that was just masked.
        state.execution_result = payload
        state.error_stack = None
        state.pending_tool_call = None
        if state.warnings:
            state.status = RunStatus.OK_WITH_WARNINGS
        elif state.retry_count > 0 or self._repair_rounds(state) > 0:
            state.status = RunStatus.SELF_HEALED
        else:
            state.status = RunStatus.FIRST_PASS
        return state.status

    @staticmethod
    def _repair_rounds(state: AgentHarnessState) -> int:
        """How many prior attempts were rejected (derived from history).

        ``retry_count`` is only incremented by the loop; a caller driving
        ``step_execution_with_harness`` by hand would leave it at 0 even after a
        repair. Deriving the count from history keeps the terminal status honest in
        both cases.
        """
        return sum(
            1
            for entry in state.history
            if entry.status not in {
                RunStatus.RUNNING.value,
                RunStatus.PENDING.value,
                RunStatus.PENDING_HUMAN_APPROVAL.value,
            }
        )

    def _suspend_for_approval(self, state: AgentHarnessState, call: ToolCall) -> RunStatus:
        """Ask a human, then continue / stop / stay pending."""
        tracer = self._tracer(state)
        if self.approval_gateway is None:
            state.status = RunStatus.PENDING_HUMAN_APPROVAL
            state.failure_reason = (
                "high-risk tool requires approval but no HumanApprovalGateway is configured"
            )
            return state.status

        span = tracer.span("hitl.approval", tool=call.tool_name)
        response: ApprovalResponse = self.approval_gateway.request_approval(
            call,
            trace_id=state.trace_id,
            task_prompt=state.task_prompt,
            reason=f"tool {call.tool_name!r} is classified as high risk",
        )
        state.approval_decision = response.decision
        state.approval_reason = response.reason
        state.metadata["approval_request_id"] = response.request_id
        tracer.end_span(span, response.decision, request_id=response.request_id)
        self._log(
            "hitl.decision",
            trace_id=state.trace_id,
            tool=call.tool_name,
            decision=response.decision,
        )

        if response.decision == "approve":
            # Approval authorises execution only; validation still applies downstream.
            result = self.execute_tool_call(state, call)
            state.metadata["last_tool_result"] = result.to_dict()
            if not result.ok:
                state.fail(
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
                return state.status
            state.execution_result = result.content
            state.pending_tool_call = None
            state.error_stack = None
            state.status = RunStatus.RUNNING
            return state.status

        if response.decision == "reject":
            state.status = RunStatus.REJECTED_BY_HUMAN
            state.failure_reason = f"human rejected {call.tool_name!r}: {response.reason}"
            state.fail(
                ErrorStack(
                    step="hitl.approval",
                    findings=[
                        Finding(
                            failed_gate="hitl.rejected",
                            message=state.failure_reason,
                            tool_name=call.tool_name,
                            resolution_hint="人工拒绝，不要重试该高风险调用",
                        )
                    ],
                ),
                status=RunStatus.REJECTED_BY_HUMAN,
            )
            return state.status

        state.status = RunStatus.PENDING_HUMAN_APPROVAL
        state.failure_reason = "awaiting human approval"
        return state.status

    # ==================================================================
    # 4. The bounded loop
    # ==================================================================
    def after_attempt(
        self,
        state: AgentHarnessState,
        status: RunStatus,
        breaker: CircuitBreaker,
        tracer: Tracer,
    ) -> Action:
        """Decide what happens after one attempt — **the only** control decision.

        Both loop drivers (``while`` and ``graph``) call this, so the retry policy
        cannot drift between them. It mutates the state (retry counter, breaker,
        terminal status) and returns the routing key.
        """
        if status.is_success:
            breaker.record_success()
            return "success"

        # A tool executed; the model gets another turn with the result.
        if status is RunStatus.RUNNING:
            return "continue"

        # Suspension is not a failure: the run is parked, not broken.
        if status in {RunStatus.PENDING_HUMAN_APPROVAL, RunStatus.REJECTED_BY_HUMAN}:
            return "suspend"

        stack = state.error_stack or ErrorStack(step="unknown")
        state.retry_count += 1
        tokens = int(state.metadata.get("last_tokens", 0))
        decision: BreakerDecision = breaker.record_failure(
            stack, attempt=state.retry_count, tokens=tokens
        )
        tracer.record_span(
            "retry",
            duration_ms=0.0,
            attempt=state.retry_count,
            signature=stack.signature,
            breaker_open=decision.open,
            code=decision.code,
        )
        if decision.open:
            state.status = RunStatus.FAILED_WITH_CIRCUIT_BREAKER
            state.failure_reason = f"{decision.code}: {decision.reason}"
            self._log(
                "circuit_breaker.open",
                trace_id=state.trace_id,
                code=decision.code,
                retry_count=state.retry_count,
            )
            return "break"

        self._log(
            "self_healing.retry",
            trace_id=state.trace_id,
            retry_count=state.retry_count,
            signature=stack.signature,
        )
        return "retry"

    def run_with_self_healing(
        self,
        state: AgentHarnessState,
        *,
        max_retries: int | None = None,
        driver: str = "while",
    ) -> HarnessRunResult:
        """Drive attempts until success, suspension, or the circuit breaker trips.

        ``driver`` selects how the loop is walked: ``"while"`` (default) or
        ``"graph"``. The decision logic is shared, so both must agree.
        """
        tracer = self.tracer or Tracer(trace_id=state.trace_id, task=state.task_prompt)
        self.tracer = tracer

        budget = max(1, max_retries if max_retries is not None else state.max_retries)
        state.max_retries = budget
        breaker = self.breaker or CircuitBreaker(max_retries=budget)
        breaker.reset()
        breaker.max_retries = budget

        ctx = LoopContext(
            engine=self,
            state=state,
            breaker=breaker,
            tracer=tracer,
            max_steps=self.max_steps,
        )
        started = time.perf_counter()
        outcome = get_driver(driver).drive(ctx)
        return self._finish(state, tracer, started, outcome.steps, outcome.status)

    def build_state(
        self,
        task_prompt: str,
        *,
        tool_whitelist: Sequence[str] | None = None,
        max_retries: int | None = None,
        trace_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentHarnessState:
        """Construct a ready-to-run state (and a matching tracer).

        Public because ``run_with_self_healing`` takes a state: callers who want to
        drive the loop themselves (or swap in a different loop driver) must be able
        to build the state without reaching into private helpers.
        """
        whitelist = (
            list(tool_whitelist)
            if tool_whitelist is not None
            else [d.name for d in self._discover_tools()]
        )
        state = AgentHarnessState(
            task_prompt=task_prompt,
            tool_whitelist=whitelist,
            max_retries=max_retries if max_retries is not None else self.max_retries,
            trace_id=trace_id or uuid.uuid4().hex,
            high_risk_tools=set(self.high_risk_tools),
        )
        if metadata:
            state.metadata.update(metadata)
        # Carry the sink over: a fresh Tracer per run must still persist traces.
        self.tracer = Tracer(
            trace_id=state.trace_id,
            task=task_prompt,
            sink=self.tracer.sink if self.tracer is not None else None,
        )
        return state

    def run(
        self,
        task_prompt: str,
        *,
        tool_whitelist: Sequence[str] | None = None,
        max_retries: int | None = None,
        trace_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        driver: str = "while",
    ) -> HarnessRunResult:
        """Convenience entry point: build the state, then run the loop."""
        state = self.build_state(
            task_prompt,
            tool_whitelist=tool_whitelist,
            max_retries=max_retries,
            trace_id=trace_id,
            metadata=metadata,
        )
        return self.run_with_self_healing(state, max_retries=max_retries, driver=driver)

    def resume(self, result: HarnessRunResult) -> HarnessRunResult:
        """Continue a suspended run after an out-of-band human decision.

        Only valid for ``PENDING_HUMAN_APPROVAL`` results. The gateway's recorded
        response is replayed, then the normal loop continues with the remaining
        step budget.
        """
        state = result.state
        if state is None or state.status is not RunStatus.PENDING_HUMAN_APPROVAL:
            raise ValueError("resume() requires a PENDING_HUMAN_APPROVAL result")

        request_id = state.metadata.get("approval_request_id")
        response = (
            self.approval_gateway.responses.get(str(request_id))
            if self.approval_gateway and request_id
            else None
        )
        if response is None:
            raise ValueError(
                "no recorded human decision for this run; call gateway.approve()/reject() first"
            )
        if response.decision == "timeout":
            # A timeout is *not* a decision. Resuming on it would silently turn
            # "nobody answered" into "rejected", which is a different outcome.
            raise ValueError(
                "no recorded human decision for this run (last response was 'timeout'); "
                "call gateway.approve()/reject() first"
            )

        tracer = self.tracer or Tracer(trace_id=state.trace_id, task=state.task_prompt)
        self.tracer = tracer
        call = state.pending_tool_call
        if call is None:
            raise ValueError("suspended run has no pending tool call")

        state.approval_decision = response.decision
        state.approval_reason = response.reason

        if response.decision != "approve":
            state.status = RunStatus.REJECTED_BY_HUMAN
            state.failure_reason = response.reason or "rejected by human"
            return self._finish(
                state, tracer, time.perf_counter(), result.attempts, state.status
            )

        exec_result = self.execute_tool_call(state, call)
        if not exec_result.ok:
            state.fail(
                ErrorStack(
                    step=f"tool.{call.tool_name}",
                    findings=[
                        Finding(
                            failed_gate=ERROR_TOOL_EXECUTION,
                            message=exec_result.error or "approved tool failed",
                            tool_name=call.tool_name,
                        )
                    ],
                )
            )
            state.pending_tool_call = None
            return self.run_with_self_healing(state, max_retries=state.max_retries)

        state.execution_result = exec_result.content
        state.pending_tool_call = None
        state.error_stack = None
        return self.run_with_self_healing(state, max_retries=state.max_retries)

    # ==================================================================
    # Prompt construction
    # ==================================================================
    def build_prompt(self, state: AgentHarnessState, *, attempt: int = 1) -> str:
        """Render the attempt prompt, including the structured repair block.

        The repair block is the *only* channel through which a failure reaches the
        model, so it is explicit about the gate, the location and the expected fix.
        """
        lines: list[str] = [
            "You are operating inside a deterministic harness.",
            f"TASK: {state.task_prompt}",
            "",
            "ALLOWED TOOLS (anything else is rejected before execution):",
        ]
        lines.extend(f"- {name}" for name in state.tool_whitelist)

        specs = self._tool_guard(state).specs
        if specs:
            lines.append("")
            lines.append("TOOL ARGUMENT SCHEMAS:")
            for name, spec in specs.items():
                lines.append(f"- {name}: {json.dumps(spec.input_schema, ensure_ascii=False)}")

        lines.append("")
        lines.append(
            'RESPONSE FORMAT: {"content": <deliverable>, '
            '"tool_call": {"tool_name": ..., "arguments": {...}} | null}'
        )
        if self.output_guard.output_schema is not None:
            lines.append(
                "OUTPUT CONTRACT: "
                + json.dumps(self.output_guard.output_schema, ensure_ascii=False)
            )
        if self.output_guard.forbidden_strings:
            lines.append("FORBIDDEN CONTENT: " + ", ".join(self.output_guard.forbidden_strings))
        if state.execution_result is not None:
            lines.append("")
            # The structural lock: tool output is fenced and labelled as data. The
            # model is never asked to guess whether a sentence came from the
            # operator or from a tool result.
            lines.append(
                wrap_untrusted(
                    json.dumps(state.execution_result, ensure_ascii=False, default=str),
                    source=str(state.metadata.get("last_phase", "tool")),
                )
            )

        if state.error_stack is not None and state.error_stack.blocking:
            lines.append("")
            lines.append(f"PREVIOUS ATTEMPT #{attempt - 1} FAILED. Fix only these problems:")
            lines.append(state.error_stack.to_prompt_block())
            if state.error_stack.trace:
                lines.append("SUPPORTING TRACE (context only):")
                lines.append(state.error_stack.trace.strip().splitlines()[-1])
            lines.append("Do not repeat the same call with the same arguments.")
        return "\n".join(lines)

    # ==================================================================
    # Internals
    # ==================================================================
    def _tracer(self, state: AgentHarnessState) -> Tracer:
        if self.tracer is None:
            self.tracer = Tracer(trace_id=state.trace_id, task=state.task_prompt)
        return self.tracer

    def _finish(
        self,
        state: AgentHarnessState,
        tracer: Tracer,
        started: float,
        attempt: int,
        status: RunStatus,
    ) -> HarnessRunResult:
        state.status = status
        duration_ms = (time.perf_counter() - started) * 1000.0
        trace = tracer.emit(
            state,
            status=status.value,
            attempts=attempt,
            ok=status.is_success,
            retry_count=state.retry_count,
        )
        result = HarnessRunResult(
            trace_id=state.trace_id,
            status=status,
            attempts=attempt,
            execution_result=state.execution_result,
            error_stack=state.error_stack,
            warnings=list(state.warnings),
            failure_reason=state.failure_reason,
            approval_request_id=state.metadata.get("approval_request_id"),
            state=state,
            trace=trace,
            duration_ms=duration_ms,
            tokens_estimate=int(trace.get("tokens_estimate", 0)),
        )
        self._log(
            "run.finished",
            trace_id=state.trace_id,
            status=status.value,
            attempts=attempt,
            retry_count=state.retry_count,
            duration_ms=round(duration_ms, 3),
            tokens_estimate=result.tokens_estimate,
        )
        return result

    def _log(self, event: str, **fields: Any) -> None:
        try:
            self.logger.info(event, **fields)
        except (ValueError, OSError):
            # Logging must never take down a run.
            return
