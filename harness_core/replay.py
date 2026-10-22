"""Record and replay the *external* half of a run.

Why record at the adapter boundary
----------------------------------
Recording at the engine level would only replay a final verdict. Recording at the
adapter boundary (``LLMAdapter.generate`` and ``MCPConnector.call_tool``) replays
the *inputs to the harness* instead, which means a replayed run executes the real
guards, the real circuit breaker and the real HITL gateway. What gets frozen is
exactly the non-deterministic part — the model and the outside world — and nothing
else.

That distinction is the whole point: a replay that skips the guards proves nothing
about the guards.

Cassette format
---------------
One JSON object per line (JSONL), append-only while recording::

    {"kind": "llm",  "seq": 0, "response": {...}}
    {"kind": "tool", "seq": 0, "tool_name": "fetch_order", "result": {...}}

Replay matches by ``kind`` and order. A replay that runs out of entries raises
:class:`ReplayExhausted` rather than inventing a response: silently returning a
default would turn "the recording is incomplete" into "the run passed", which is
the exact failure mode a CI gate must not have.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .adapters.llm import LLMResponse, ToolCallRequest
from .adapters.mcp import ToolDefinition, ToolResult
from .errors import HarnessInfrastructureError

#: Bumped whenever the cassette schema changes in a way replay must notice.
CASSETTE_VERSION = 1

#: The version assumed when a cassette has no header line at all. v0 is the format
#: written before the header existed: a bare sequence of entry objects.
LEGACY_VERSION = 0


class ReplayError(HarnessInfrastructureError):
    """Base class for replay failures.

    Subclasses :class:`HarnessInfrastructureError` so a broken cassette escapes the
    self-healing loop: retrying cannot fix a truncated recording, and reporting it as
    a task failure would make a broken fixture look like a real verdict.
    """


class CassetteVersionError(ReplayError):
    """The cassette's version cannot be read, or cannot be migrated.

    Separate from :class:`ReplayError` because the remedy is different: a broken
    cassette means "re-record", a version problem means "upgrade or migrate".
    """


class ReplayExhausted(ReplayError):
    """The recording ended before the run did."""


class CassetteMismatch(ReplayError):
    """The recording does not match what the run asked for."""


@dataclass
class CassetteEntry:
    kind: str  # "llm" | "tool"
    seq: int
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "seq": self.seq, **self.payload}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CassetteEntry":
        if "kind" not in data:
            raise ReplayError(f"cassette entry is missing 'kind': {data!r}")
        if "seq" not in data:
            # A truncated or hand-edited cassette must fail with a clear message,
            # not a KeyError from deep inside the loader.
            raise ReplayError(f"cassette entry is missing 'seq': {data!r}")
        payload = {key: value for key, value in data.items() if key not in {"kind", "seq"}}
        return cls(kind=str(data["kind"]), seq=int(data["seq"]), payload=payload)


class Cassette:
    """An ordered list of recorded adapter interactions."""

    def __init__(self, entries: Sequence[CassetteEntry] | None = None) -> None:
        self.entries: list[CassetteEntry] = list(entries or [])
        self._cursors: dict[str, int] = {}

    # ------------------------------------------------------------------ io
    @classmethod
    def load(cls, path: str | Path, *, allow_migration: bool = True) -> "Cassette":
        """Read a cassette, migrating an older one when needed.

        The version is read from the header and *checked*. An earlier version of
        this loader skipped any line without ``kind``, which meant a cassette
        written by a future version — whose entries might have a different shape —
        would be silently parsed as if it were current. That is the failure this
        check exists to prevent: a wrong answer that looks like a right one.

        ``allow_migration=False`` refuses anything that is not exactly current, for
        callers that must not silently accept an upgraded recording.
        """
        target = Path(path)
        if not target.exists():
            raise ReplayError(f"cassette not found: {target}")

        raw_lines = target.read_text(encoding="utf-8").splitlines()
        version, body_start = _read_header(raw_lines, target)

        if version != CASSETTE_VERSION:
            if not allow_migration:
                raise CassetteVersionError(
                    f"{target}: cassette is version {version}, this build reads "
                    f"{CASSETTE_VERSION} (migration disabled)"
                )
            version = migrate(target, version)

        entries: list[CassetteEntry] = []
        for offset, text in enumerate(raw_lines[body_start:], start=body_start + 1):
            stripped = text.strip()
            if not stripped:
                continue
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ReplayError(
                    f"{target}:{offset}: invalid JSON in cassette: {exc}"
                ) from exc
            if "kind" not in data:
                # Only the header may lack `kind`; anything else is malformed and
                # must not be skipped as though it were a version marker.
                raise ReplayError(
                    f"{target}:{offset}: entry without 'kind': {stripped[:120]!r}"
                )
            entries.append(CassetteEntry.from_dict(data))
        return cls(entries)

    @property
    def version(self) -> int:
        """The schema version this cassette is written in."""
        return CASSETTE_VERSION

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps({"cassette_version": CASSETTE_VERSION}) + "\n")
            for entry in self.entries:
                handle.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")

    # ----------------------------------------------------------------- api
    def next_entry(self, kind: str) -> CassetteEntry:
        """Pop the next entry of ``kind`` (order-preserving per kind)."""
        cursor = self._cursors.get(kind, 0)
        matching = [entry for entry in self.entries if entry.kind == kind]
        if cursor >= len(matching):
            raise ReplayExhausted(
                f"cassette has {len(matching)} {kind!r} entries but the run asked for "
                f"#{cursor + 1}; the recording is incomplete"
            )
        self._cursors[kind] = cursor + 1
        return matching[cursor]

    def rewind(self) -> None:
        self._cursors.clear()

    def count(self, kind: str | None = None) -> int:
        if kind is None:
            return len(self.entries)
        return sum(1 for entry in self.entries if entry.kind == kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cassette_version": CASSETTE_VERSION,
            "entries": [entry.to_dict() for entry in self.entries],
        }


# ---------------------------------------------------------------------------
# recording
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# versioning
# ---------------------------------------------------------------------------


def _read_header(lines: Sequence[str], target: Path) -> tuple[int, int]:
    """Return ``(version, index_of_first_entry_line)``.

    A cassette with no header is a legacy v0 cassette — that is the format written
    before the header existed, so absence is meaningful rather than an error.
    """
    for index, text in enumerate(lines):
        stripped = text.strip()
        if not stripped:
            continue
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            # A corrupt first line is reported by the caller's entry loop, with a
            # line number; here we only need to know there is no usable header.
            return LEGACY_VERSION, 0
        if not isinstance(data, dict):
            return LEGACY_VERSION, 0
        if "cassette_version" in data:
            try:
                return int(data["cassette_version"]), index + 1
            except (TypeError, ValueError) as exc:
                raise CassetteVersionError(
                    f"{target}:{index + 1}: unreadable cassette_version "
                    f"{data['cassette_version']!r}"
                ) from exc
        # A first line with no version marker: a legacy cassette whose body starts
        # at line 0. Do not consume it as a header.
        return LEGACY_VERSION, 0
    return LEGACY_VERSION, 0


#: One migration function per version, applied in order until current.
#: Each takes a parsed cassette document and returns the upgraded document.
_MIGRATIONS: dict[int, "Callable[[dict[str, Any]], dict[str, Any]]"] = {}


def register_migration(
    from_version: int,
) -> "Callable[[Callable[[dict[str, Any]], dict[str, Any]]], Callable[[dict[str, Any]], dict[str, Any]]]":
    """Register the step that upgrades ``from_version`` to ``from_version + 1``."""

    def decorator(
        fn: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> Callable[[dict[str, Any]], dict[str, Any]]:
        _MIGRATIONS[from_version] = fn
        return fn

    return decorator


@register_migration(0)
def _migrate_v0_to_v1(document: dict[str, Any]) -> dict[str, Any]:
    """v0 → v1.

    v0 is the headerless format: a bare list of entries. v1 adds the version header
    and nothing else, so the payload needs no rewriting — the upgrade is purely
    "stamp the version". Keeping it explicit (rather than special-casing "v0 is
    just v1 without a header") means a future v2 has an obvious place to live.
    """
    return {"cassette_version": 1, "entries": list(document.get("entries") or [])}


def migrate(path: str | Path, from_version: int) -> int:
    """Upgrade the cassette at ``path`` in place; return the resulting version.

    In place on purpose: a replay that silently used an un-upgraded file would keep
    working until someone changed the schema again, which is exactly how migration
    debt becomes an outage. The file on disk ends up at the current version, so the
    next load is a plain read.
    """
    target = Path(path)
    version = from_version
    if version > CASSETTE_VERSION:
        raise CassetteVersionError(
            f"{target}: cassette is version {version}, newer than this build "
            f"({CASSETTE_VERSION}); upgrade harness-agent-core instead of migrating "
            "downwards"
        )

    document = _read_document(target)
    while version < CASSETTE_VERSION:
        step = _MIGRATIONS.get(version)
        if step is None:
            raise CassetteVersionError(
                f"{target}: no migration registered from version {version}; "
                f"re-record the cassette with this build"
            )
        document = step(document)
        version += 1

    _write_document(target, document, version)
    return version


def _read_document(target: Path) -> dict[str, Any]:
    """Parse any cassette version into a normalised ``{"entries": [...]}`` dict."""
    entries: list[dict[str, Any]] = []
    for text in target.read_text(encoding="utf-8").splitlines():
        stripped = text.strip()
        if not stripped:
            continue
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            continue  # a corrupt line is the entry loop's problem, not migration's
        if isinstance(data, dict) and "kind" in data:
            entries.append(data)
    return {"cassette_version": LEGACY_VERSION, "entries": entries}


def _write_document(target: Path, document: dict[str, Any], version: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"cassette_version": version}) + "\n")
        for entry in document.get("entries") or []:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


@dataclass
class Recorder:
    """Collects adapter interactions in memory; write with :meth:`save`."""

    entries: list[CassetteEntry] = field(default_factory=list)
    _seq: dict[str, int] = field(default_factory=dict)

    def _next_seq(self, kind: str) -> int:
        value = self._seq.get(kind, 0)
        self._seq[kind] = value + 1
        return value

    def record_llm(self, response: LLMResponse) -> None:
        self.entries.append(
            CassetteEntry(
                kind="llm",
                seq=self._next_seq("llm"),
                payload={"response": response.to_dict()},
            )
        )

    def record_tool(self, result: ToolResult) -> None:
        self.entries.append(
            CassetteEntry(
                kind="tool",
                seq=self._next_seq("tool"),
                payload={
                    "tool_name": result.tool_name,
                    "result": result.to_dict(),
                },
            )
        )

    def to_cassette(self) -> Cassette:
        return Cassette(list(self.entries))

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        self.to_cassette().save(target)
        return target


# ---------------------------------------------------------------------------
# replaying adapters
# ---------------------------------------------------------------------------


class RecordingLLMAdapter:
    """Wraps any :class:`LLMAdapter`, forwarding calls and recording responses."""

    def __init__(self, inner: Any, recorder: Recorder) -> None:
        self._inner = inner
        self._recorder = recorder
        self.name = f"recording({getattr(inner, 'name', 'unknown')})"

    def generate(
        self,
        prompt: str,
        state: Any,
        *,
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        response: LLMResponse = self._inner.generate(prompt, state, tools=tools)
        self._recorder.record_llm(response)
        return response

    def __getattr__(self, item: str) -> Any:
        # Prompts etc. stay reachable so tests can assert on the wrapped adapter.
        return getattr(self._inner, item)


class RecordingMCPConnector:
    """Wraps any :class:`MCPConnector`, forwarding calls and recording results."""

    def __init__(self, inner: Any, recorder: Recorder) -> None:
        self._inner = inner
        self._recorder = recorder
        self.name = f"recording({getattr(inner, 'name', 'unknown')})"

    def list_tools(self) -> list[ToolDefinition]:
        tools: list[ToolDefinition] = self._inner.list_tools()
        return tools

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        result: ToolResult = self._inner.call_tool(tool_name, arguments)
        self._recorder.record_tool(result)
        return result

    def close(self) -> None:
        self._inner.close()

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)


class ReplayLLMAdapter:
    """Serves recorded responses instead of calling a model."""

    name = "replay"

    def __init__(self, cassette: Cassette) -> None:
        self._cassette = cassette
        self.prompts: list[str] = []

    def generate(
        self,
        prompt: str,
        state: Any,
        *,
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        self.prompts.append(prompt)
        entry = self._cassette.next_entry("llm")
        payload = entry.payload.get("response") or {}
        tool_call = payload.get("tool_call")
        return LLMResponse(
            content=payload.get("content"),
            tool_call=(
                ToolCallRequest(
                    tool_name=str(tool_call.get("tool_name", "")),
                    arguments=dict(tool_call.get("arguments") or {}),
                )
                if tool_call
                else None
            ),
            raw_text=str(payload.get("raw_text", "")),
            tokens_estimate=int(payload.get("tokens_estimate", 0)),
            model=str(payload.get("model", "replay")),
            error=payload.get("error"),
        )


class ReplayMCPConnector:
    """Serves recorded tool results instead of executing anything.

    ``list_tools`` is reconstructed from the recorded calls, so a replay cannot
    advertise a capability the recording never used.
    """

    name = "replay-mcp"

    def __init__(self, cassette: Cassette) -> None:
        self._cassette = cassette
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_tools(self) -> list[ToolDefinition]:
        names: list[str] = []
        for entry in self._cassette.entries:
            if entry.kind != "tool":
                continue
            name = str(entry.payload.get("tool_name", ""))
            if name and name not in names:
                names.append(name)
        return [ToolDefinition(name=name) for name in names]

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        self.calls.append((tool_name, dict(arguments)))
        entry = self._cassette.next_entry("tool")
        recorded_name = str(entry.payload.get("tool_name", ""))
        if recorded_name and recorded_name != tool_name:
            raise CassetteMismatch(
                f"cassette expected tool {recorded_name!r} but the run called "
                f"{tool_name!r}; the recording belongs to a different scenario"
            )
        payload = entry.payload.get("result") or {}
        return ToolResult(
            tool_name=tool_name,
            ok=bool(payload.get("ok", False)),
            content=payload.get("content"),
            error=payload.get("error"),
            duration_ms=float(payload.get("duration_ms", 0.0)),
            meta=dict(payload.get("meta") or {}),
        )

    def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# high-level helpers
# ---------------------------------------------------------------------------


@dataclass
class ReplayReport:
    """Comparison between a recorded outcome and its replay."""

    cassette_path: str
    recorded_status: str
    replayed_status: str
    recorded_attempts: int
    replayed_attempts: int
    recorded_retries: int
    replayed_retries: int
    llm_calls: int
    tool_calls: int

    @property
    def matches(self) -> bool:
        return (
            self.recorded_status == self.replayed_status
            and self.recorded_attempts == self.replayed_attempts
            and self.recorded_retries == self.replayed_retries
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "cassette": self.cassette_path,
            "matches": self.matches,
            "recorded": {
                "status": self.recorded_status,
                "attempts": self.recorded_attempts,
                "retries": self.recorded_retries,
            },
            "replayed": {
                "status": self.replayed_status,
                "attempts": self.replayed_attempts,
                "retries": self.replayed_retries,
            },
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
        }


def summarize(cassette: Cassette) -> dict[str, Any]:
    """Cheap stats for a cassette, used by ``harness-core replay --inspect``."""
    llm = cassette.count("llm")
    tool = cassette.count("tool")
    tools: list[str] = []
    for entry in cassette.entries:
        if entry.kind == "tool":
            name = str(entry.payload.get("tool_name", ""))
            if name and name not in tools:
                tools.append(name)
    return {"entries": cassette.count(), "llm_calls": llm, "tool_calls": tool, "tools": tools}


def iter_kinds(entries: Iterable[CassetteEntry]) -> list[str]:
    return [entry.kind for entry in entries]
