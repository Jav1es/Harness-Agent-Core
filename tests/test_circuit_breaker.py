"""Circuit breaker tests (unit level, no engine involved)."""

from __future__ import annotations

from harness_core.circuit_breaker import (
    BREAKER_BUDGET,
    BREAKER_CONSECUTIVE,
    BREAKER_MAX_RETRIES,
    BREAKER_RECURRED,
    CircuitBreaker,
)
from harness_core.errors import ErrorStack, Finding


def _stack(gate: str = "g", severity: str = "BLOCK") -> ErrorStack:
    return ErrorStack(step="s", findings=[Finding(failed_gate=gate, message="m", severity=severity)])  # type: ignore[arg-type]


def test_closed_below_the_cap() -> None:
    breaker = CircuitBreaker(max_retries=3)
    assert breaker.record_failure(_stack("a"), attempt=1).closed
    assert breaker.record_failure(_stack("b"), attempt=2).closed


def test_opens_at_the_cap() -> None:
    breaker = CircuitBreaker(max_retries=2)
    breaker.record_failure(_stack("a"), attempt=1)
    decision = breaker.record_failure(_stack("b"), attempt=2)
    assert decision.open
    assert decision.code == BREAKER_MAX_RETRIES


def test_opens_on_repeated_signature() -> None:
    breaker = CircuitBreaker(max_retries=10)
    breaker.record_failure(_stack("a"), attempt=1)
    decision = breaker.record_failure(_stack("a"), attempt=2)
    assert decision.open
    assert decision.code == BREAKER_RECURRED
    assert "recurred" in decision.reason


def test_repeat_threshold_is_configurable() -> None:
    breaker = CircuitBreaker(max_retries=10, repeat_threshold=2)
    assert breaker.record_failure(_stack("a"), attempt=1).closed
    assert breaker.record_failure(_stack("a"), attempt=2).closed
    decision = breaker.record_failure(_stack("a"), attempt=3)
    assert decision.open and decision.code == BREAKER_RECURRED


def test_different_signatures_do_not_trip_the_repeat_breaker() -> None:
    breaker = CircuitBreaker(max_retries=10)
    for index in range(5):
        assert breaker.record_failure(_stack(f"gate{index}"), attempt=index + 1).closed
    assert breaker.distinct_failure_modes == 5


def test_token_budget_trips() -> None:
    breaker = CircuitBreaker(max_retries=10, token_budget=100)
    breaker.record_failure(_stack("a"), attempt=1, tokens=60)
    decision = breaker.record_failure(_stack("b"), attempt=2, tokens=60)
    assert decision.open
    assert decision.code == BREAKER_BUDGET


def test_consecutive_failure_cap() -> None:
    breaker = CircuitBreaker(max_retries=10, max_consecutive_failures=2)
    breaker.record_failure(_stack("a"), attempt=1)
    decision = breaker.record_failure(_stack("b"), attempt=2)
    assert decision.open
    assert decision.code == BREAKER_CONSECUTIVE


def test_success_resets_the_consecutive_counter() -> None:
    breaker = CircuitBreaker(max_retries=10, max_consecutive_failures=2)
    breaker.record_failure(_stack("a"), attempt=1)
    breaker.record_success()
    assert breaker.consecutive_failures == 0
    assert breaker.record_failure(_stack("b"), attempt=2).closed


def test_warn_only_stack_has_no_signature() -> None:
    breaker = CircuitBreaker(max_retries=5)
    warn = _stack("w", severity="WARN")
    assert warn.signature == ""
    # A stack with no blocking findings cannot trigger the repeat breaker.
    assert breaker.record_failure(warn, attempt=1).closed


def test_reset_clears_state() -> None:
    breaker = CircuitBreaker(max_retries=3)
    breaker.record_failure(_stack("a"), attempt=1, tokens=10)
    breaker.reset()
    assert breaker.total_failures == 0
    assert breaker.tokens_spent == 0
    assert breaker.distinct_failure_modes == 0


def test_decision_truthiness() -> None:
    breaker = CircuitBreaker(max_retries=1)
    assert breaker.record_failure(_stack("a"), attempt=1)
