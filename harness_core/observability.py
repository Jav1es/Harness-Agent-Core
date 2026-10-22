"""Observability: trace ids, spans, token/time accounting and a JSONL sink.

The tracer is intentionally framework-free. LangGraph / OpenTelemetry / Opik are
all adapters *over* this interface, not requirements — the CI gate must work with
zero infrastructure.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol

#: Roughly 4 characters per token. Deliberately crude and deterministic: a real
#: tokenizer is an optional adapter, but CI numbers must be reproducible offline.
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str | None) -> int:
    """Deterministic token estimate (never calls a tokenizer, never networks)."""
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN)


def estimate_payload_tokens(payload: Any) -> int:
    if payload is None:
        return 0
    if isinstance(payload, str):
        return estimate_tokens(payload)
    try:
        return estimate_tokens(json.dumps(payload, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return estimate_tokens(repr(payload))


class TraceSink(Protocol):
    """Anything that can persist one finished trace record."""

    def write(self, record: dict[str, Any]) -> None: ...


class JsonlSink:
    """Append-only JSONL writer. One line per run; safe for CI artefacts."""

    def __init__(self, path: str | Path, *, truncate: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if truncate:
            self.path.write_text("", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


class MemorySink:
    """In-memory sink used by tests and by ``eval``."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write(self, record: dict[str, Any]) -> None:
        self.records.append(record)


@dataclass
class TraceSpan:
    """One timed unit of work (a step, a tool call, a repair round)."""

    name: str
    trace_id: str
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    parent_id: str | None = None
    started_at: float = field(default_factory=time.perf_counter)
    ended_at: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"
    tokens_estimate: int = 0

    @property
    def duration_ms(self) -> float:
        end = self.ended_at if self.ended_at is not None else time.perf_counter()
        return (end - self.started_at) * 1000.0

    def finish(
        self,
        status: str = "ok",
        *,
        tokens_estimate: int | None = None,
        **attributes: Any,
    ) -> "TraceSpan":
        self.ended_at = time.perf_counter()
        self.status = status
        if tokens_estimate is not None:
            self.tokens_estimate = tokens_estimate
        self.attributes.update(attributes)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 3),
            "tokens_estimate": self.tokens_estimate,
            "attributes": self.attributes,
        }


class Tracer:
    """Collects spans for one ``trace_id`` and emits a single JSONL record."""

    def __init__(
        self,
        trace_id: str | None = None,
        *,
        sink: TraceSink | None = None,
        task: str = "",
    ) -> None:
        self.trace_id = trace_id or uuid.uuid4().hex
        self.sink = sink
        self.task = task
        self.spans: list[TraceSpan] = []
        self._stack: list[TraceSpan] = []
        self.started_at = time.perf_counter()

    # ---------------------------------------------------------------- spans
    def span(self, name: str, **attributes: Any) -> TraceSpan:
        parent = self._stack[-1].span_id if self._stack else None
        span = TraceSpan(name=name, trace_id=self.trace_id, parent_id=parent)
        span.attributes.update(attributes)
        self.spans.append(span)
        self._stack.append(span)
        return span

    def end_span(self, span: TraceSpan, status: str = "ok", **attributes: Any) -> None:
        span.finish(status, **attributes)
        if self._stack and self._stack[-1] is span:
            self._stack.pop()

    def record_span(self, name: str, duration_ms: float, **attributes: Any) -> TraceSpan:
        """Record an already-measured span (used for tool calls timed elsewhere)."""
        parent = self._stack[-1].span_id if self._stack else None
        span = TraceSpan(name=name, trace_id=self.trace_id, parent_id=parent)
        span.ended_at = span.started_at + duration_ms / 1000.0
        span.attributes.update(attributes)
        self.spans.append(span)
        return span

    # ---------------------------------------------------------------- stats
    @property
    def total_duration_ms(self) -> float:
        return (time.perf_counter() - self.started_at) * 1000.0

    @property
    def tokens_estimate(self) -> int:
        return sum(span.tokens_estimate for span in self.spans)

    def span_count(self, name: str | None = None) -> int:
        if name is None:
            return len(self.spans)
        return sum(1 for span in self.spans if span.name == name)

    # ---------------------------------------------------------------- output
    def to_record(self, state: Any = None, **extra: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "trace_id": self.trace_id,
            "task": self.task,
            "total_duration_ms": round(self.total_duration_ms, 3),
            "tokens_estimate": self.tokens_estimate,
            "span_count": len(self.spans),
            "spans": [span.to_dict() for span in self.spans],
        }
        if state is not None and hasattr(state, "to_dict"):
            record["state"] = state.to_dict()
        record.update(extra)
        return record

    def emit(self, state: Any = None, **extra: Any) -> dict[str, Any]:
        """Build the record and write it to the configured sink (if any)."""
        record = self.to_record(state, **extra)
        if self.sink is not None:
            self.sink.write(record)
        return record


class StructuredLogger:
    """Tiny structured logger: JSON lines on stderr by default.

    Deliberately not ``logging``: the harness needs machine-readable records with
    a stable schema, and CI artefacts are easier to diff as JSONL.
    """

    def __init__(self, *, stream: Any = None, verbose: bool = False, enabled: bool = True) -> None:
        import sys

        self.stream = stream if stream is not None else sys.stderr
        self.verbose = verbose
        self.enabled = enabled

    def log(self, level: str, event: str, **fields: Any) -> None:
        if not self.enabled:
            return
        if not self.verbose and level == "debug":
            return
        payload = {"level": level, "event": event, **fields}
        self.stream.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    def debug(self, event: str, **fields: Any) -> None:
        self.log("debug", event, **fields)

    def info(self, event: str, **fields: Any) -> None:
        self.log("info", event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self.log("warning", event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self.log("error", event, **fields)


def summarize_spans(spans: Iterable[TraceSpan]) -> dict[str, Any]:
    """Aggregate helper used by the CLI report."""
    by_name: dict[str, int] = {}
    tokens = 0
    duration = 0.0
    for span in spans:
        by_name[span.name] = by_name.get(span.name, 0) + 1
        tokens += span.tokens_estimate
        duration += span.duration_ms
    return {
        "span_kinds": by_name,
        "tokens_estimate": tokens,
        "duration_ms": round(duration, 3),
    }
