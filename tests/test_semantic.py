"""The semantic layer: model-backed checks with honest cost accounting.

The tests are written around the two things that make this layer different from the
deterministic one, and the two things that can go wrong because of them:

* it costs money → the spend must be recorded and attributable;
* it can be wrong or unavailable → an unverified payload must never read as passed.
"""

from __future__ import annotations

import pytest

from harness_core.adapters.llm import FakeLLMAdapter, LLMResponse
from harness_core.engine import HarnessOrchestrationEngine
from harness_core.errors import ERROR_SEMANTIC, Finding
from harness_core.observability import MemorySink, StructuredLogger, Tracer
from harness_core.scenarios import ORDER_OUTPUT_SCHEMA
from harness_core.semantic import (
    DEFAULT_MAX_CHECKS,
    LLMJudgeGate,
    SemanticLayer,
    SemanticVerdict,
    build_judge,
)


def judge_response(ok: bool, reason: str = "because", tokens: int = 10) -> LLMResponse:
    return LLMResponse(
        content={"ok": ok, "reason": reason},
        raw_text='{"ok": %s, "reason": "%s"}' % (str(ok).lower(), reason),
        tokens_estimate=tokens,
        model="judge",
    )


class FixedGate:
    """A gate with a predetermined verdict, for testing the layer not the judge."""

    name = "fixed"

    def __init__(self, ok: bool, reason: str = "", tokens: int = 5) -> None:
        self.verdict = SemanticVerdict(ok=ok, reason=reason, tokens=tokens)
        self.calls = 0

    def evaluate(self, payload, requirement):
        self.calls += 1
        return self.verdict


class ExplodingGate:
    name = "exploding"

    def evaluate(self, payload, requirement):
        raise RuntimeError("judge is down")


# ---------------------------------------------------------------------------
# the judge itself
# ---------------------------------------------------------------------------


def test_judge_parses_a_json_answer() -> None:
    llm = FakeLLMAdapter([judge_response(True, "looks fine", tokens=37)])
    verdict = LLMJudgeGate(llm).evaluate({"a": 1}, "is it fine?")
    assert verdict.ok is True
    assert verdict.reason == "looks fine"
    assert verdict.tokens == 37


def test_judge_reads_json_out_of_prose() -> None:
    """Models like to add a sentence before the JSON; that must still parse."""
    llm = FakeLLMAdapter(
        [LLMResponse(content='Sure! {"ok": false, "reason": "missing citation"}')]
    )
    verdict = LLMJudgeGate(llm).evaluate({}, "cite the source")
    assert verdict.ok is False
    assert "citation" in verdict.reason


def test_judge_accepts_string_booleans() -> None:
    llm = FakeLLMAdapter([LLMResponse(content='{"ok": "yes", "reason": "ok"}')])
    assert LLMJudgeGate(llm).evaluate({}, "x").ok is True


def test_unparseable_judge_answer_fails_closed() -> None:
    """A judge that ignores its own output contract is not evidence of quality."""
    llm = FakeLLMAdapter([LLMResponse(content="I think it is probably fine")])
    verdict = LLMJudgeGate(llm).evaluate({}, "x")
    assert verdict.ok is False
    assert "unparseable" in verdict.reason


def test_judge_error_fails_closed() -> None:
    llm = FakeLLMAdapter([LLMResponse(error="model refused")])
    verdict = LLMJudgeGate(llm).evaluate({}, "x")
    assert verdict.ok is False
    assert "judge failed" in verdict.reason


def test_judge_truncates_a_huge_payload() -> None:
    llm = FakeLLMAdapter([judge_response(True)])
    gate = LLMJudgeGate(llm, max_payload_chars=50)
    gate.evaluate({"blob": "x" * 5000}, "is it ok?")
    assert "truncated" in llm.prompts[0]


def test_judge_prompt_contains_the_requirement_verbatim() -> None:
    llm = FakeLLMAdapter([judge_response(True)])
    LLMJudgeGate(llm).evaluate({"a": 1}, "must cite document D-7")
    assert "must cite document D-7" in llm.prompts[0]


# ---------------------------------------------------------------------------
# the layer
# ---------------------------------------------------------------------------


def test_a_passing_requirement_produces_no_finding() -> None:
    layer = SemanticLayer()
    layer.add("cites", "must cite a source", FixedGate(True))
    assert layer.check({"a": 1}) == []
    assert layer.checks_run == 1


def test_a_failing_requirement_produces_a_finding_with_its_own_gate_name() -> None:
    """The gate name must be distinguishable from a deterministic failure."""
    layer = SemanticLayer()
    layer.add("cites", "must cite a source", FixedGate(False, "no citation"))
    findings = layer.check({})
    assert len(findings) == 1
    assert findings[0].failed_gate == f"{ERROR_SEMANTIC}.cites"
    assert findings[0].failed_gate.startswith("semantic.")
    assert not findings[0].failed_gate.startswith("output.schema")
    assert findings[0].message == "no citation"


def test_tokens_are_accounted() -> None:
    layer = SemanticLayer()
    layer.add("a", "req a", FixedGate(True, tokens=12))
    layer.add("b", "req b", FixedGate(True, tokens=30))
    layer.check({})
    assert layer.tokens_spent == 42
    assert layer.checks_run == 2


def test_budget_exhaustion_blocks_instead_of_skipping() -> None:
    """The central rule: "could not verify" must never read as "verified"."""
    layer = SemanticLayer(max_checks=1)
    layer.add("a", "req a", FixedGate(True))
    layer.add("b", "req b", FixedGate(True))
    findings = layer.check({})
    assert len(findings) == 1
    assert findings[0].failed_gate == f"{ERROR_SEMANTIC}.budget"
    assert "never evaluated" in findings[0].message
    assert findings[0].severity == "BLOCK"


def test_budget_does_not_call_the_gate() -> None:
    gate = FixedGate(True)
    layer = SemanticLayer(max_checks=1)
    layer.add("a", "req a", gate)
    layer.add("b", "req b", gate)
    layer.check({})
    assert gate.calls == 1, "the second gate must not be paid for"


def test_an_exploding_gate_does_not_pass_silently() -> None:
    layer = SemanticLayer()
    layer.add("boom", "req", ExplodingGate())
    findings = layer.check({})
    assert len(findings) == 1
    assert "RuntimeError" in findings[0].message
    assert findings[0].severity == "BLOCK"


def test_reset_clears_the_meter() -> None:
    layer = SemanticLayer()
    layer.add("a", "req", FixedGate(True, tokens=9))
    layer.check({})
    layer.reset()
    assert layer.tokens_spent == 0 and layer.checks_run == 0


def test_report_shape() -> None:
    layer = SemanticLayer(max_checks=3)
    layer.add("a", "req", FixedGate(True, tokens=7))
    layer.check({})
    report = layer.to_dict()
    assert report["requirements"] == ["a"]
    assert report["max_checks"] == 3
    assert report["checks_run"] == 1
    assert report["tokens_spent"] == 7
    assert report["gates_evaluated"] == ["a"]


def test_build_judge_makes_one_gate_per_requirement() -> None:
    llm = FakeLLMAdapter([judge_response(True), judge_response(True)])
    layer = build_judge(llm, [("a", "req a"), ("b", "req b")])
    assert [r.name for r in layer.requirements] == ["a", "b"]
    assert layer.max_checks == DEFAULT_MAX_CHECKS
    layer.check({})
    assert layer.checks_run == 2


# ---------------------------------------------------------------------------
# integration with the engine
# ---------------------------------------------------------------------------


def _engine(llm, **kwargs) -> HarnessOrchestrationEngine:
    from harness_core.adapters.mcp import FakeMCPConnector

    return HarnessOrchestrationEngine(
        llm=llm,
        connector=FakeMCPConnector(),
        output_schema=ORDER_OUTPUT_SCHEMA,
        logger=StructuredLogger(enabled=False),
        **kwargs,
    )


VALID = {
    "order_id": "ORD-1001",
    "currency": "CNY",
    "amount": 1200.0,
    "status": "settled",
}


def test_a_passing_semantic_check_keeps_the_run_green() -> None:
    layer = SemanticLayer()
    layer.add("cites", "must cite a source", FixedGate(True))
    llm = FakeLLMAdapter([LLMResponse(content=dict(VALID))])
    result = _engine(llm, semantic_layer=layer).run("settle")
    assert result.ok
    assert result.status.value == "FIRST_PASS"


def test_a_failing_semantic_check_blocks_the_run() -> None:
    layer = SemanticLayer()
    layer.add("cites", "must cite a source", FixedGate(False, "no citation"))
    llm = FakeLLMAdapter([LLMResponse(content=dict(VALID))])
    result = _engine(llm, semantic_layer=layer, max_retries=0).run("settle")
    assert not result.ok
    gates = [f.failed_gate for f in result.error_stack.findings]
    assert any(g.startswith("semantic.") for g in gates)


def test_the_semantic_layer_is_skipped_when_deterministic_checks_fail() -> None:
    """No point paying a judge to review a payload that already failed a schema."""
    gate = FixedGate(True)
    layer = SemanticLayer()
    layer.add("cites", "req", gate)
    llm = FakeLLMAdapter([LLMResponse(content={"order_id": "ORD-1001"})])
    result = _engine(llm, semantic_layer=layer, max_retries=0).run("settle")
    assert not result.ok
    assert gate.calls == 0
    assert layer.tokens_spent == 0


def test_semantic_spend_is_reported_in_state_metadata() -> None:
    layer = SemanticLayer()
    layer.add("a", "req", FixedGate(True, tokens=25))
    llm = FakeLLMAdapter([LLMResponse(content=dict(VALID))])
    engine = _engine(llm, semantic_layer=layer)
    state = engine.build_state("settle")
    engine.run_with_self_healing(state)
    assert state.metadata["semantic"]["tokens_spent"] == 25
    assert state.metadata["semantic"]["checks_run"] == 1


def test_semantic_spend_produces_a_trace_span() -> None:
    sink = MemorySink()
    layer = SemanticLayer()
    layer.add("a", "req", FixedGate(True, tokens=13))
    llm = FakeLLMAdapter([LLMResponse(content=dict(VALID))])
    engine = _engine(llm, semantic_layer=layer, tracer=Tracer(sink=sink))
    engine.run("settle")
    spans = [s for s in sink.records[-1]["spans"] if s["name"] == "semantic"]
    assert spans, [s["name"] for s in sink.records[-1]["spans"]]
    assert spans[0]["attributes"].get("tokens_spent") == 13


def test_a_semantic_failure_consumes_a_retry_like_any_other_block() -> None:
    """A BLOCK is a BLOCK: the repair loop gets a chance to fix it."""
    layer = SemanticLayer()
    layer.add("cites", "must cite", FixedGate(False, "no citation"))
    llm = FakeLLMAdapter([LLMResponse(content=dict(VALID)), LLMResponse(content=dict(VALID))])
    result = _engine(llm, semantic_layer=layer, max_retries=1).run("settle")
    assert not result.ok
    assert result.state is not None
    assert result.state.retry_count == 1


def test_no_layer_means_no_semantic_span() -> None:
    sink = MemorySink()
    llm = FakeLLMAdapter([LLMResponse(content=dict(VALID))])
    _engine(llm, tracer=Tracer(sink=sink)).run("settle")
    assert not [s for s in sink.records[-1]["spans"] if s["name"] == "semantic"]


def test_an_empty_layer_is_a_no_op() -> None:
    sink = MemorySink()
    llm = FakeLLMAdapter([LLMResponse(content=dict(VALID))])
    _engine(llm, semantic_layer=SemanticLayer(), tracer=Tracer(sink=sink)).run("settle")
    assert not [s for s in sink.records[-1]["spans"] if s["name"] == "semantic"]


# ---------------------------------------------------------------------------
# the finding type is the same one everywhere
# ---------------------------------------------------------------------------


def test_semantic_findings_are_ordinary_findings() -> None:
    layer = SemanticLayer()
    layer.add("a", "req", FixedGate(False, "nope"))
    findings = layer.check({})
    assert isinstance(findings[0], Finding)
    assert findings[0].fingerprint, "a semantic finding must be fingerprintable too"


def test_warn_severity_does_not_block() -> None:
    layer = SemanticLayer()
    layer.add("style", "req", FixedGate(False, "style nit"), severity="WARN")
    findings = layer.check({})
    assert findings[0].severity == "WARN"


def test_duplicate_judgements_are_deterministically_fingerprinted() -> None:
    """Same requirement failing twice must produce the same fingerprint, so the
    breaker can recognise a recurring failure instead of retrying forever."""
    gate = FixedGate(False, "same reason")
    layer = SemanticLayer()
    layer.add("a", "req", gate)
    first = layer.check({})[0].fingerprint
    second = layer.check({})[0].fingerprint
    assert first == second


@pytest.mark.parametrize("severity", ["BLOCK", "WARN", "INFO"])
def test_all_severities_are_accepted(severity: str) -> None:
    layer = SemanticLayer()
    layer.add("a", "req", FixedGate(False), severity=severity)  # type: ignore[arg-type]
    findings = layer.check({})
    assert findings[0].severity == severity
