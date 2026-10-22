"""Observability tests: trace fields, spans, token estimate, JSONL sink."""

from __future__ import annotations

import json
from pathlib import Path

from harness_core.observability import (
    JsonlSink,
    MemorySink,
    StructuredLogger,
    TraceSpan,
    Tracer,
    estimate_payload_tokens,
    estimate_tokens,
)

from .helpers import VALID, response_for, tool_response


def test_trace_id_is_generated_per_run(engine_factory) -> None:
    engine, _ = engine_factory([response_for(), response_for()])
    first = engine.run("task a", tool_whitelist=["fetch_order"])
    second = engine.run("task b", tool_whitelist=["fetch_order"])
    assert first.trace_id != second.trace_id
    assert len(first.trace_id) >= 16


def test_trace_records_every_required_field(engine_factory, sink: MemorySink) -> None:
    engine, _ = engine_factory([response_for()])
    result = engine.run("task", tool_whitelist=["fetch_order"])
    assert sink.records, "the tracer must emit exactly one record per run"

    record = sink.records[-1]
    for key in ("trace_id", "task", "total_duration_ms", "tokens_estimate", "span_count", "spans"):
        assert key in record, f"missing trace field {key}"
    assert record["trace_id"] == result.trace_id

    span = record["spans"][0]
    for key in ("span_id", "name", "status", "duration_ms", "tokens_estimate"):
        assert key in span, f"missing span field {key}"


def test_trace_records_tool_name_and_status(engine_factory, sink: MemorySink) -> None:
    engine, _ = engine_factory([tool_response("fetch_order", {"order_id": "ORD-1"}), response_for()])
    engine.run("task", tool_whitelist=["fetch_order"])

    record = sink.records[-1]
    tool_spans = [span for span in record["spans"] if span["name"] == "tool.call"]
    assert len(tool_spans) == 1
    assert tool_spans[0]["attributes"]["tool"] == "fetch_order"
    assert tool_spans[0]["status"] == "ok"


def test_trace_records_retry_count_and_attempts(engine_factory, sink: MemorySink) -> None:
    engine, _ = engine_factory([response_for({**VALID, "currency": "EUR"}), response_for()])
    engine.run("task", tool_whitelist=["fetch_order"])

    record = sink.records[-1]
    assert record["retry_count"] == 1
    assert record["attempts"] == 2
    assert record["status"] == "SELF_HEALED"
    retry_spans = [span for span in record["spans"] if span["name"] == "retry"]
    assert len(retry_spans) == 1


def test_trace_embeds_state_snapshot(engine_factory, sink: MemorySink) -> None:
    engine, _ = engine_factory([response_for()])
    engine.run("task", tool_whitelist=["fetch_order"])
    state = sink.records[-1]["state"]
    assert state["status"] == "FIRST_PASS"
    assert state["tool_whitelist"] == ["fetch_order"]
    assert state["max_retries"] == 3
    assert isinstance(state["history"], list)


def test_jsonl_sink_writes_one_line_per_run(tmp_path: Path) -> None:
    path = tmp_path / "traces" / "run.jsonl"
    sink = JsonlSink(path)
    tracer = Tracer(sink=sink, task="t")
    tracer.span("x").finish()
    tracer.emit()
    tracer.emit()

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    for line in lines:
        payload = json.loads(line)
        assert payload["trace_id"] == tracer.trace_id


def test_jsonl_sink_truncate(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    JsonlSink(path).write({"a": 1})
    JsonlSink(path, truncate=True).write({"b": 2})
    assert path.read_text(encoding="utf-8").strip() == '{"b": 2}'


def test_token_estimate_is_deterministic() -> None:
    assert estimate_tokens("abcdefgh") == 2
    assert estimate_tokens("") == 0
    assert estimate_payload_tokens({"a": 1}) == estimate_payload_tokens({"a": 1})


def test_span_duration_is_measured() -> None:
    span = TraceSpan(name="x", trace_id="t")
    span.finish()
    assert span.duration_ms >= 0
    assert span.status == "ok"


def test_tracer_counts_spans_and_tokens() -> None:
    tracer = Tracer(task="t")
    tracer.span("a").finish(tokens_estimate=10)
    tracer.span("b").finish(tokens_estimate=5)
    assert tracer.span_count() == 2
    assert tracer.span_count("a") == 1
    assert tracer.tokens_estimate == 15


def test_structured_logger_can_be_disabled() -> None:
    import io

    buffer = io.StringIO()
    StructuredLogger(stream=buffer, enabled=False).info("event", a=1)
    assert buffer.getvalue() == ""

    enabled = io.StringIO()
    StructuredLogger(stream=enabled, enabled=True).info("event", a=1)
    assert json.loads(enabled.getvalue().strip())["event"] == "event"


def test_no_span_leaks_into_the_next_run(engine_factory, sink: MemorySink) -> None:
    engine, _ = engine_factory([response_for(), response_for()])
    engine.run("task 1", tool_whitelist=["fetch_order"])
    engine.run("task 2", tool_whitelist=["fetch_order"])
    assert len(sink.records) == 2
    assert sink.records[0]["trace_id"] != sink.records[1]["trace_id"]
