"""Engine tests: guard_tool_call, deterministic_validator, self-healing, breaker."""

from __future__ import annotations

from harness_core.errors import ERROR_TOOL_NOT_ALLOWED, ERROR_TOOL_SCHEMA
from harness_core.state import AgentHarnessState, RunStatus, ToolCall

from .helpers import VALID, response_for, tool_response


# ---------------------------------------------------------------------------
# guard_tool_call — pure authorisation
# ---------------------------------------------------------------------------


def test_guard_tool_call_denies_unwhitelisted_tool(engine_factory) -> None:
    engine, _ = engine_factory()
    state = AgentHarnessState(task_prompt="t", tool_whitelist=["fetch_order"])
    call = ToolCall(tool_name="delete_resource", arguments={"id": "1"})
    findings = engine.guard_tool_call(state, call)
    assert len(findings) == 1
    assert findings[0].failed_gate == ERROR_TOOL_NOT_ALLOWED
    assert call.decision == "deny"
    assert engine.connector.call_count() == 0  # type: ignore[attr-defined]


def test_guard_tool_call_denies_bad_arguments(engine_factory) -> None:
    engine, _ = engine_factory()
    state = AgentHarnessState(task_prompt="t", tool_whitelist=["fetch_order"])
    call = ToolCall(tool_name="fetch_order", arguments={"wrong": 1})
    findings = engine.guard_tool_call(state, call)
    assert findings[0].failed_gate.startswith(ERROR_TOOL_SCHEMA)
    assert call.decision == "deny"


def test_guard_tool_call_allows_whitelisted_tool(engine_factory) -> None:
    engine, _ = engine_factory()
    state = AgentHarnessState(task_prompt="t", tool_whitelist=["fetch_order"])
    call = ToolCall(tool_name="fetch_order", arguments={"order_id": "ORD-1"})
    assert engine.guard_tool_call(state, call) == []
    assert call.decision == "allow"


def test_guard_tool_call_marks_high_risk_for_approval(engine_factory) -> None:
    engine, _ = engine_factory()
    state = AgentHarnessState(task_prompt="t", tool_whitelist=["write_api"])
    call = ToolCall(tool_name="write_api", arguments={"order_id": "ORD-1", "amount": 1})
    assert engine.guard_tool_call(state, call) == []
    assert call.decision == "require_approval"


# ---------------------------------------------------------------------------
# deterministic_validator — (ok, message)
# ---------------------------------------------------------------------------


def test_deterministic_validator_accepts_valid_payload(engine_factory) -> None:
    engine, _ = engine_factory()
    ok, message = engine.deterministic_validator(VALID)
    assert ok is True
    assert message == ""


def test_deterministic_validator_reports_schema_failure(engine_factory) -> None:
    engine, _ = engine_factory()
    ok, message = engine.deterministic_validator({**VALID, "currency": "EUR"})
    assert ok is False
    assert "output.schema.enum" in message
    assert "currency" in message


def test_deterministic_validator_returns_actionable_message(engine_factory) -> None:
    engine, _ = engine_factory()
    ok, message = engine.deterministic_validator({"order_id": "ORD-1"})
    assert ok is False
    # The message is fed back verbatim, so it must name the missing fields.
    for field in ("currency", "amount", "status"):
        assert field in message


def test_deterministic_validator_is_pluggable(engine_factory) -> None:
    engine, _ = engine_factory()
    engine.register_validator("must_be_cny", lambda p: p.get("currency") == "CNY")
    ok, message = engine.deterministic_validator({**VALID, "currency": "USD"})
    assert ok is False
    assert "must_be_cny" in message


def test_validator_returning_string_uses_it_as_hint(engine_factory) -> None:
    engine, _ = engine_factory()
    engine.register_validator("custom", lambda p: "amount 必须等于 1200")
    ok, message = engine.deterministic_validator(VALID)
    assert ok is False
    assert "amount 必须等于 1200" in message


def test_broken_validator_becomes_a_finding_not_a_crash(engine_factory) -> None:
    engine, _ = engine_factory()

    def boom(_payload):
        raise ValueError("bad rule")

    engine.register_validator("boom", boom)
    ok, message = engine.deterministic_validator(VALID)
    assert ok is False
    assert "eval_error" in message


def test_none_payload_is_rejected(engine_factory) -> None:
    engine, _ = engine_factory()
    ok, message = engine.deterministic_validator(None)
    assert ok is False
    assert "no deliverable" in message


# ---------------------------------------------------------------------------
# self-healing
# ---------------------------------------------------------------------------


def test_first_pass_succeeds_without_retry(engine_factory) -> None:
    engine, llm = engine_factory([response_for()])
    result = engine.run("task", tool_whitelist=["fetch_order"])
    assert result.status is RunStatus.FIRST_PASS
    assert result.state is not None and result.state.retry_count == 0
    assert llm.prompt_count == 1


def test_self_healing_feeds_errors_back_and_succeeds(engine_factory) -> None:
    broken = {**VALID, "amount": 0.0}
    engine, llm = engine_factory([response_for(broken), response_for()])
    result = engine.run("task", tool_whitelist=["fetch_order"])

    assert result.status is RunStatus.SELF_HEALED
    assert result.ok is True
    assert llm.prompt_count == 2
    # The second prompt must contain the structured failure block.
    repair_prompt = llm.prompts[1]
    assert "PREVIOUS ATTEMPT" in repair_prompt
    assert "output.business_rule.amount_positive" in repair_prompt


def test_self_healing_records_history(engine_factory) -> None:
    broken = {**VALID, "currency": "EUR"}
    engine, _ = engine_factory([response_for(broken), response_for()])
    result = engine.run("task", tool_whitelist=["fetch_order"])
    assert result.state is not None
    history = result.state.history
    assert len(history) == 2
    assert history[0].status == RunStatus.FAILED.value
    assert history[1].status == RunStatus.SELF_HEALED.value
    assert history[0].error_summary is not None


def test_tool_call_then_deliverable_does_not_burn_retries(engine_factory) -> None:
    engine, llm = engine_factory(
        [tool_response("fetch_order", {"order_id": "ORD-1001"}), response_for()]
    )
    result = engine.run("task", tool_whitelist=["fetch_order"])
    assert result.status is RunStatus.FIRST_PASS
    assert result.state is not None and result.state.retry_count == 0
    assert engine.connector.call_count("fetch_order") == 1  # type: ignore[attr-defined]
    # The second prompt must carry the tool result, fenced as untrusted data.
    assert "ORD-1001" in llm.prompts[1]
    assert "UNTRUSTED DATA" in llm.prompts[1]
    assert "END UNTRUSTED DATA" in llm.prompts[1]


def test_denied_tool_is_never_executed(engine_factory) -> None:
    engine, _ = engine_factory(
        [tool_response("drop_database", {"db": "x"}), response_for()]
    )
    result = engine.run("task", tool_whitelist=["fetch_order"])
    assert result.ok
    assert engine.connector.call_count("drop_database") == 0  # type: ignore[attr-defined]


def test_tool_execution_error_is_repaired(engine_factory) -> None:
    def explode(_args):
        raise RuntimeError("upstream 503")

    engine, llm = engine_factory([tool_response("fetch_order", {"order_id": "ORD-1"}), response_for()])
    engine.connector.add_tool("fetch_order", explode, input_schema=engine.tool_specs["fetch_order"].input_schema)  # type: ignore[attr-defined]
    result = engine.run("task", tool_whitelist=["fetch_order"])
    assert result.status is RunStatus.SELF_HEALED
    assert "upstream 503" in llm.prompts[1]


def test_llm_adapter_exception_is_a_structured_failure(engine_factory) -> None:
    from harness_core.adapters.llm import FakeLLMAdapter

    class ExplodingLLM(FakeLLMAdapter):
        def generate(self, prompt, state, *, tools=None):  # type: ignore[override]
            raise RuntimeError("provider down")

    engine, _ = engine_factory(llm=ExplodingLLM([]))
    result = engine.run("task", tool_whitelist=["fetch_order"])
    assert not result.ok
    assert result.error_stack is not None
    assert "provider down" in result.error_stack.to_prompt_block()


# ---------------------------------------------------------------------------
# circuit breaker
# ---------------------------------------------------------------------------


def test_max_retries_exhausted_returns_circuit_breaker(engine_factory) -> None:
    engine, llm = engine_factory(
        [
            response_for({**VALID, "order_id": "X"}),
            response_for({**VALID, "currency": "EUR"}),
            response_for({**VALID, "status": "unknown"}),
        ],
        max_retries=3,
    )
    result = engine.run("task", tool_whitelist=["fetch_order"])
    assert result.status is RunStatus.FAILED_WITH_CIRCUIT_BREAKER
    assert result.exit_code == 1
    assert "GXT_MAX_RETRIES_EXHAUSTED" in (result.failure_reason or "")
    assert result.state is not None and result.state.retry_count == 3
    assert llm.prompt_count == 3
    assert result.error_stack is not None and result.error_stack.blocking


def test_repeated_failure_trips_earlier_than_the_cap(engine_factory) -> None:
    payload = {**VALID, "currency": "EUR"}
    engine, llm = engine_factory([response_for(payload)] * 5, max_retries=5)
    result = engine.run("task", tool_whitelist=["fetch_order"])
    assert result.status is RunStatus.FAILED_WITH_CIRCUIT_BREAKER
    assert "GXT_FINDINGS_RECURRED" in (result.failure_reason or "")
    assert llm.prompt_count == 2


def test_distinct_failures_do_not_trip_the_repeat_breaker(engine_factory) -> None:
    engine, _ = engine_factory(
        [
            response_for({**VALID, "order_id": "X"}),
            response_for({**VALID, "currency": "EUR"}),
        ],
        max_retries=2,
    )
    result = engine.run("task", tool_whitelist=["fetch_order"])
    assert "GXT_FINDINGS_RECURRED" not in (result.failure_reason or "")
    assert "GXT_MAX_RETRIES_EXHAUSTED" in (result.failure_reason or "")


def test_max_steps_prevents_an_infinite_tool_loop(engine_factory) -> None:
    # The model keeps asking for a tool and never produces a deliverable.
    engine, llm = engine_factory(
        [tool_response("fetch_order", {"order_id": "ORD-1001"})], max_steps=3
    )
    result = engine.run("task", tool_whitelist=["fetch_order"])
    assert not result.ok
    assert result.status is RunStatus.FAILED_WITH_CIRCUIT_BREAKER
    assert "max_steps" in (result.failure_reason or "")
    assert llm.prompt_count == 3


# ---------------------------------------------------------------------------
# prompt construction
# ---------------------------------------------------------------------------


def test_build_prompt_lists_whitelist_and_schemas(engine_factory) -> None:
    engine, _ = engine_factory()
    state = AgentHarnessState(task_prompt="settle order", tool_whitelist=["fetch_order"])
    prompt = engine.build_prompt(state)
    assert "settle order" in prompt
    assert "fetch_order" in prompt
    assert "order_id" in prompt
    assert "OUTPUT CONTRACT" in prompt


def test_build_prompt_is_stable_for_the_same_state(engine_factory) -> None:
    engine, _ = engine_factory()
    state = AgentHarnessState(task_prompt="x", tool_whitelist=["fetch_order"])
    assert engine.build_prompt(state) == engine.build_prompt(state)
