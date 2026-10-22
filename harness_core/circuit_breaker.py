"""Circuit breakers for the self-healing loop.

Two independent trip conditions, because they answer different questions:

1. **Retry budget exhausted** — "we spent everything we had". Cheap to reason
   about, but wasteful: three identical failures burn three rounds of tokens to
   learn nothing.
2. **Repeated failure signature** — "the model is not actually repairing". The
   fingerprint set of the BLOCK findings is identical to a previous attempt, so
   another round would pay for the same failure. This trips *earlier* and is the
   more interesting control.

A third, coarser guard (consecutive failures across attempts) is kept for callers
that drive the engine manually.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ErrorStack

#: Machine-readable trip codes. They end up in traces and in CI reports.
BREAKER_MAX_RETRIES = "GXT_MAX_RETRIES_EXHAUSTED"
BREAKER_RECURRED = "GXT_FINDINGS_RECURRED"
BREAKER_CONSECUTIVE = "GXT_CONSECUTIVE_FAILURES"
BREAKER_BUDGET = "GXT_TOKEN_BUDGET_EXHAUSTED"


@dataclass(frozen=True)
class BreakerDecision:
    """The breaker's verdict after one failed attempt."""

    open: bool
    code: str = ""
    reason: str = ""

    def __bool__(self) -> bool:  # allows ``if breaker_decision:``
        return self.open

    @property
    def closed(self) -> bool:
        return not self.open


CLOSED = BreakerDecision(open=False)


@dataclass
class CircuitBreaker:
    """Tracks failure signatures and decides when retrying stops being rational.

    Parameters
    ----------
    max_retries:
        Hard cap on repair rounds.
    repeat_threshold:
        How many times the *same* BLOCK signature may appear before tripping.
        ``1`` means "the second time we see it, stop" (default). Raise it if a
        flaky tool legitimately needs two identical attempts.
    max_consecutive_failures:
        Optional coarse safety net for manual drivers. ``0`` disables it (default),
        because ``max_retries`` already bounds the loop; enabling a *lower* limit
        than ``max_retries`` would silently change which trip code you observe.
    token_budget:
        Optional ceiling on estimated tokens. ``0`` disables the check.
    """

    max_retries: int = 3
    repeat_threshold: int = 1
    max_consecutive_failures: int = 0
    token_budget: int = 0
    seen_signatures: dict[str, int] = field(default_factory=dict)
    #: signature -> the attempt number it was first observed at (for trace messages).
    first_seen: dict[str, int] = field(default_factory=dict)
    consecutive_failures: int = 0
    total_failures: int = 0
    tokens_spent: int = 0

    def reset(self) -> None:
        self.seen_signatures.clear()
        self.first_seen.clear()
        self.consecutive_failures = 0
        self.total_failures = 0
        self.tokens_spent = 0

    # ------------------------------------------------------------------ API
    def record_failure(self, stack: ErrorStack, *, attempt: int, tokens: int = 0) -> BreakerDecision:
        """Account for one failed attempt and return the breaker verdict.

        Precedence: repeated signature first (it is the cheapest signal to act
        on), then budget, then the retry cap.
        """
        self.total_failures += 1
        self.consecutive_failures += 1
        self.tokens_spent += max(0, tokens)

        signature = stack.signature
        if signature:
            count = self.seen_signatures.get(signature, 0) + 1
            self.seen_signatures[signature] = count
            self.first_seen.setdefault(signature, attempt)
            if count > self.repeat_threshold:
                first_seen = self.first_seen[signature]
                return BreakerDecision(
                    open=True,
                    code=BREAKER_RECURRED,
                    reason=(
                        f"same {len(stack.blocking)} blocking finding(s) recurred at "
                        f"attempt {attempt} (first seen at attempt {first_seen}); "
                        f"repair is not converging, tokens_spent={self.tokens_spent}"
                    ),
                )

        if self.token_budget and self.tokens_spent >= self.token_budget:
            return BreakerDecision(
                open=True,
                code=BREAKER_BUDGET,
                reason=(
                    f"token budget {self.token_budget} exhausted "
                    f"(spent {self.tokens_spent} at attempt {attempt})"
                ),
            )

        if attempt >= self.max_retries:
            return BreakerDecision(
                open=True,
                code=BREAKER_MAX_RETRIES,
                reason=(
                    f"max_retries={self.max_retries} exhausted; "
                    f"tokens_spent={self.tokens_spent}; "
                    f"distinct_failure_modes={len(self.seen_signatures)}"
                ),
            )

        if self.max_consecutive_failures and (
            self.consecutive_failures >= self.max_consecutive_failures
        ):
            return BreakerDecision(
                open=True,
                code=BREAKER_CONSECUTIVE,
                reason=(
                    f"{self.consecutive_failures} consecutive failures "
                    f"(limit {self.max_consecutive_failures})"
                ),
            )

        return CLOSED

    def record_success(self) -> None:
        self.consecutive_failures = 0

    @property
    def distinct_failure_modes(self) -> int:
        return len(self.seen_signatures)
