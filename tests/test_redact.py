"""Output-side redaction: remove the secret, don't just reject the run.

The distinction this file is built around
-----------------------------------------
A rejection gate (``forbidden_patterns``) and a redactor look similar and behave
oppositely when a secret leaks:

* rejection sends the payload back to the model, which cannot see the problem and
  will paraphrase — a paraphrase of a secret is still a secret;
* redaction removes it, records the event, and lets the run finish.

The tests therefore check three separate things: that the secret is **gone from the
deliverable**, that the event is **visible**, and that the run is **not** blocked.
"""

from __future__ import annotations

import pytest

from harness_core.adapters.llm import FakeLLMAdapter, LLMResponse
from harness_core.adapters.mcp import FakeMCPConnector
from harness_core.engine import HarnessOrchestrationEngine
from harness_core.observability import MemorySink, StructuredLogger, Tracer
from harness_core.redact import (
    DEFAULT_REDACTORS,
    ERROR_REDACTED,
    OutputRedactor,
    Redactor,
    mask_for,
    redact,
)

VALID = {"order_id": "ORD-1001", "currency": "CNY", "amount": 1200.0, "status": "settled"}

#: A schema with a free-text field, so a secret has somewhere realistic to appear.
#: ``ORDER_OUTPUT_SCHEMA`` sets ``additionalProperties: false`` and would reject the
#: payload before redaction ever got a chance to matter — the test would then pass
#: or fail for the wrong reason.
NOTES_SCHEMA = {
    "type": "object",
    "required": ["order_id", "currency", "amount", "status"],
    "properties": {
        "order_id": {"type": "string", "minLength": 3},
        "currency": {"type": "string", "enum": ["CNY", "USD"]},
        "amount": {"type": "number", "minimum": 0},
        "status": {"type": "string", "enum": ["settled", "rejected"]},
        "notes": {"type": "string"},
    },
}

CARD = "4111111111111111"      # passes Luhn
CARD_SPACED = "4111 1111 1111 1111"
CARD_BAD = "1234567890123456"  # fails Luhn


# ---------------------------------------------------------------------------
# what gets caught
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("sk-abcdefghijklmnopqrstuvwx", "api_key"),
        ("pk-abcdefghijklmnopqrstuvwx", "api_key"),
        ("Bearer eyJhbGciOiJIUzI1NiJ9abcdefgh", "bearer_token"),
        (CARD, "card_number"),
        (CARD_SPACED, "card_number"),
        ("4111-1111-1111-1111", "card_number"),
        ("user@example.com", "email"),
        ("13812345678", "phone_cn"),
        ("110101199003071234", "cn_id"),
        ("-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----", "private_key"),
    ],
)
def test_each_kind_is_redacted(text: str, kind: str) -> None:
    out, findings = redact({"v": text})
    assert out["v"] == mask_for(kind)
    assert [f.failed_gate for f in findings] == [f"{ERROR_REDACTED}.{kind}"]


def test_the_original_value_is_gone() -> None:
    """The one assertion that actually matters: the secret must not survive."""
    out, _ = redact({"notes": f"card on file: {CARD}"})
    assert CARD not in out["notes"]
    assert "REDACTED" in out["notes"]


# ---------------------------------------------------------------------------
# what must NOT be caught
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "hello world",
        "order ORD-1001 settled",
        "amount 1200.00 CNY",
        CARD_BAD,                       # fails Luhn: not a card number
        "trace 0f1e2d3c4b5a6978",       # a hex id, not a card
        "2024-11-05",                   # a date
    ],
)
def test_ordinary_content_is_untouched(text: str) -> None:
    out, findings = redact({"v": text})
    assert out["v"] == text
    assert findings == []


def test_a_luhn_invalid_number_is_not_redacted() -> None:
    """Without Luhn, every 16-digit id would be masked and the feature would be off."""
    out, findings = redact({"v": CARD_BAD})
    assert out["v"] == CARD_BAD
    assert findings == []


def test_the_luhn_check_is_what_rejects_it() -> None:
    from harness_core.redact import _card_like

    assert _card_like(CARD) is True
    assert _card_like(CARD_BAD) is False


# ---------------------------------------------------------------------------
# structure handling
# ---------------------------------------------------------------------------


def test_nested_payloads_are_walked() -> None:
    out, findings = redact({"a": {"b": [f"x {CARD} y"]}})
    assert CARD not in str(out)
    assert findings


def test_the_location_is_reported() -> None:
    _, findings = redact({"order": {"notes": f"card {CARD}"}})
    assert "order.notes" in findings[0].message


def test_the_list_index_is_reported() -> None:
    _, findings = redact({"items": ["ok", f"card {CARD}"]})
    assert "items[1]" in findings[0].message


def test_non_string_values_are_left_alone() -> None:
    out, findings = redact({"amount": 1200.0, "count": 3, "ok": True, "nil": None})
    assert out == {"amount": 1200.0, "count": 3, "ok": True, "nil": None}
    assert findings == []


def test_multiple_kinds_are_counted_separately() -> None:
    _, findings = redact(
        {"a": f"{CARD} and user@example.com", "b": "sk-abcdefghijklmnopqrstuvwx"}
    )
    kinds = {f.failed_gate.split(".")[-1] for f in findings}
    assert kinds == {"card_number", "email", "api_key"}


def test_repeated_values_are_counted() -> None:
    _, findings = redact({"a": f"{CARD} {CARD}"})
    assert "2 card_number" in findings[0].message


def test_spacing_is_preserved_around_the_mask() -> None:
    """A pattern that swallows the trailing space glues words onto the mask."""
    out, _ = redact({"v": f"x {CARD} y"})
    assert out["v"] == "x [REDACTED:card_number] y"


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def test_disabled_means_disabled() -> None:
    out, findings = OutputRedactor(enabled=False).redact({"v": "sk-abcdefghijklmnopqrstuvwx"})
    assert out["v"] == "sk-abcdefghijklmnopqrstuvwx"
    assert findings == []


def test_field_scoping_limits_the_walk() -> None:
    redactor = OutputRedactor(fields=["notes"])
    out, _ = redactor.redact({"notes": "user@example.com", "keep": "user@example.com"})
    assert out["notes"] == mask_for("email")
    assert out["keep"] == "user@example.com"


def test_a_custom_replacement_is_used() -> None:
    redactor = OutputRedactor(
        redactors=[Redactor("ssn", r"\b\d{3}-\d{2}-\d{4}\b", replacement="***")]
    )
    out, _ = redactor.redact({"v": "ssn 123-45-6789"})
    assert out["v"] == "ssn ***"


def test_a_broken_pattern_is_skipped_not_fatal() -> None:
    """A bad regex must not abort a run: the forbidden-pattern gate reports config errors."""
    redactor = OutputRedactor(redactors=[Redactor("broken", r"(unclosed")])
    out, findings = redactor.redact({"v": "text"})
    assert out["v"] == "text"
    assert findings == []


def test_an_accept_hook_can_reject_a_match() -> None:
    redactor = OutputRedactor(
        redactors=[Redactor("nines", r"\b9+\b", accept=lambda t: len(t) > 3)]
    )
    out, _ = redactor.redact({"v": "9 9999"})
    assert out["v"] == "9 [REDACTED:nines]"


def test_the_default_redactor_set_is_not_empty() -> None:
    assert DEFAULT_REDACTORS
    assert {r.kind for r in DEFAULT_REDACTORS} >= {"api_key", "email", "card_number"}


# ---------------------------------------------------------------------------
# findings are WARN, not BLOCK
# ---------------------------------------------------------------------------


def test_redaction_is_a_warning() -> None:
    """The leak was handled; blocking would report a problem that no longer exists."""
    _, findings = redact({"v": f"{CARD}"})
    assert findings[0].severity == "WARN"


def test_the_gate_prefix_is_distinct_from_forbidden() -> None:
    _, findings = redact({"v": f"{CARD}"})
    assert findings[0].failed_gate.startswith("output.redacted.")
    assert not findings[0].failed_gate.startswith("output.forbidden")


def test_the_finding_does_not_contain_the_secret() -> None:
    """A report that quotes the secret is a second leak.

    This is enforced structurally rather than by care: ``Redactor.to_finding`` takes
    only a count and field locations, never the matched text, so there is no
    variable in scope that could carry the secret into the message. The test pins
    that property so a future refactor cannot quietly pass the match through.
    """
    secret = "sk-abcdefghijklmnopqrstuvwx"
    _, findings = redact({"v": secret})
    assert secret not in findings[0].message
    assert secret not in (findings[0].resolution_hint or "")


def test_to_finding_cannot_receive_the_matched_text() -> None:
    """The signature is the guarantee, not the implementation."""
    import inspect

    params = list(inspect.signature(Redactor.to_finding).parameters)
    assert params == ["self", "count", "locations"]


# ---------------------------------------------------------------------------
# engine integration
# ---------------------------------------------------------------------------


def _engine(llm, **kwargs) -> HarnessOrchestrationEngine:
    return HarnessOrchestrationEngine(
        llm=llm,
        connector=FakeMCPConnector(),
        output_schema=NOTES_SCHEMA,
        logger=StructuredLogger(enabled=False),
        **kwargs,
    )


def test_no_redactor_means_no_change() -> None:
    llm = FakeLLMAdapter([LLMResponse(content=dict(VALID))])
    result = _engine(llm).run("settle")
    assert result.ok
    assert result.execution_result == VALID


def test_the_deliverable_is_the_redacted_payload() -> None:
    """Storing the original would undo the redaction for every downstream consumer."""
    payload = {**VALID, "notes": f"card {CARD}"}
    llm = FakeLLMAdapter([LLMResponse(content=payload)])
    result = _engine(llm, redactor=OutputRedactor()).run("settle")
    assert result.ok
    assert CARD not in str(result.execution_result)
    assert "REDACTED" in str(result.execution_result)


def test_redaction_does_not_block_the_run() -> None:
    payload = {**VALID, "notes": f"card {CARD}"}
    llm = FakeLLMAdapter([LLMResponse(content=payload)])
    result = _engine(llm, redactor=OutputRedactor()).run("settle")
    assert result.ok
    assert result.status.value in ("OK_WITH_WARNINGS", "SELF_HEALED", "FIRST_PASS")


def test_redaction_is_reported_as_a_warning() -> None:
    payload = {**VALID, "notes": f"card {CARD}"}
    llm = FakeLLMAdapter([LLMResponse(content=payload)])
    result = _engine(llm, redactor=OutputRedactor()).run("settle")
    assert any(f.failed_gate.startswith("output.redacted.") for f in result.warnings)


def test_each_redaction_is_reported_once() -> None:
    """A finding appended on two paths reports the same leak twice.

    That is not a cosmetic issue: an operator counting warnings would conclude two
    secrets leaked when only one did, and the deduplicated metadata would disagree
    with the warning list.
    """
    payload = {**VALID, "notes": f"card {CARD}"}
    llm = FakeLLMAdapter([LLMResponse(content=payload)])
    result = _engine(llm, redactor=OutputRedactor()).run("settle")
    redaction_warnings = [
        f.failed_gate for f in result.warnings if f.failed_gate.startswith("output.redacted.")
    ]
    assert redaction_warnings == ["output.redacted.card_number"]


def test_redactions_are_recorded_in_metadata() -> None:
    payload = {**VALID, "notes": f"card {CARD}"}
    llm = FakeLLMAdapter([LLMResponse(content=payload)])
    result = _engine(llm, redactor=OutputRedactor()).run("settle")
    assert result.state is not None
    assert result.state.metadata.get("redactions")


def test_redaction_happens_before_the_forbidden_pattern_gate() -> None:
    """A forbidden pattern that matches a secret must not block: it was removed.

    If redaction ran after validation, the run would be rejected over a string that
    was about to be deleted — the operator would see "blocked" and never learn why.
    """
    payload = {**VALID, "notes": f"card {CARD}"}
    llm = FakeLLMAdapter([LLMResponse(content=payload)])
    engine = _engine(llm, redactor=OutputRedactor())
    engine.output_guard.forbidden_patterns = (r"\d{16}",)
    result = engine.run("settle")
    assert result.ok, result.failure_reason
    assert not any(
        f.failed_gate == "output.forbidden_pattern" for f in result.warnings
    )


def test_without_redaction_the_forbidden_pattern_still_blocks() -> None:
    """Control for the test above: the pattern does block when nothing redacts."""
    payload = {**VALID, "notes": f"card {CARD}"}
    llm = FakeLLMAdapter([LLMResponse(content=payload), LLMResponse(content=payload)])
    engine = _engine(llm)
    engine.output_guard.forbidden_patterns = (r"\d{16}",)
    result = engine.run("settle")
    assert not result.ok


def test_redaction_produces_a_trace_span() -> None:
    sink = MemorySink()
    payload = {**VALID, "notes": f"card {CARD}"}
    llm = FakeLLMAdapter([LLMResponse(content=payload)])
    _engine(llm, redactor=OutputRedactor(), tracer=Tracer(sink=sink)).run("settle")
    spans = [s for s in sink.records[-1]["spans"] if s["name"] == "redact"]
    assert spans, [s["name"] for s in sink.records[-1]["spans"]]


def test_no_redact_span_without_a_redactor() -> None:
    sink = MemorySink()
    llm = FakeLLMAdapter([LLMResponse(content=dict(VALID))])
    _engine(llm, tracer=Tracer(sink=sink)).run("settle")
    assert not [s for s in sink.records[-1]["spans"] if s["name"] == "redact"]


def test_a_clean_payload_creates_no_redaction_warning() -> None:
    llm = FakeLLMAdapter([LLMResponse(content=dict(VALID))])
    result = _engine(llm, redactor=OutputRedactor()).run("settle")
    assert not any(f.failed_gate.startswith("output.redacted.") for f in result.warnings)
