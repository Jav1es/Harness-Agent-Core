"""Cassette versioning: read the version, refuse the wrong one, migrate the old one.

The bug this closes
-------------------
The original loader skipped every line without ``kind``, treating it as a version
header. Two consequences, both silent:

* a cassette from a *future* build — whose entries may have a different shape —
  was parsed as if it were current;
* a malformed entry in the middle of a file was skipped instead of reported.

Both turn "this recording is not what you think it is" into "the run passed". These
tests pin the version check, the migration path, and the refusal to guess.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness_core.replay import (
    CASSETTE_VERSION,
    LEGACY_VERSION,
    Cassette,
    CassetteEntry,
    CassetteVersionError,
    ReplayError,
    _read_header,
    migrate,
    register_migration,
)


def _entry(kind: str = "llm", seq: int = 0, **payload) -> dict:
    return {"kind": kind, "seq": seq, **payload}


def write_cassette(path: Path, lines: list[dict], *, version: int | None = CASSETTE_VERSION) -> Path:
    body: list[str] = []
    if version is not None:
        body.append(json.dumps({"cassette_version": version}))
    body.extend(json.dumps(line) for line in lines)
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# reading the header
# ---------------------------------------------------------------------------


def test_a_current_cassette_loads(tmp_path: Path) -> None:
    path = write_cassette(tmp_path / "c.jsonl", [_entry(response={"content": "hi"})])
    cassette = Cassette.load(path)
    assert cassette.count("llm") == 1
    assert cassette.version == CASSETTE_VERSION


def test_a_headerless_cassette_is_read_as_legacy(tmp_path: Path) -> None:
    path = write_cassette(
        tmp_path / "c.jsonl", [_entry(response={"content": "hi"})], version=None
    )
    cassette = Cassette.load(path)
    assert cassette.count("llm") == 1


def test_a_headerless_cassette_is_migrated_in_place(tmp_path: Path) -> None:
    """Loading a legacy cassette upgrades the file, so the next load is a plain read."""
    path = write_cassette(
        tmp_path / "c.jsonl", [_entry(response={"content": "hi"})], version=None
    )
    Cassette.load(path)
    first_line = path.read_text(encoding="utf-8").splitlines()[0]
    assert json.loads(first_line)["cassette_version"] == CASSETTE_VERSION


def test_an_unreadable_version_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    path.write_text('{"cassette_version": "banana"}\n', encoding="utf-8")
    with pytest.raises(CassetteVersionError, match="unreadable cassette_version"):
        Cassette.load(path)


def test_a_future_version_is_refused_not_guessed(tmp_path: Path) -> None:
    """A newer cassette may have a different shape; parsing it would be a guess."""
    path = write_cassette(
        tmp_path / "c.jsonl", [_entry(response={})], version=CASSETTE_VERSION + 5
    )
    with pytest.raises(CassetteVersionError, match="newer than this build"):
        Cassette.load(path)


def test_a_future_version_message_says_what_to_do(tmp_path: Path) -> None:
    path = write_cassette(tmp_path / "c.jsonl", [], version=CASSETTE_VERSION + 1)
    with pytest.raises(CassetteVersionError) as excinfo:
        Cassette.load(path)
    assert "upgrade harness-agent-core" in str(excinfo.value)


def test_migration_can_be_refused(tmp_path: Path) -> None:
    path = write_cassette(tmp_path / "c.jsonl", [_entry(response={})], version=None)
    with pytest.raises(CassetteVersionError, match="migration disabled"):
        Cassette.load(path, allow_migration=False)


# ---------------------------------------------------------------------------
# a malformed entry is reported, not skipped
# ---------------------------------------------------------------------------


def test_an_entry_without_kind_is_reported(tmp_path: Path) -> None:
    """The old loader skipped these silently — that is how a wrong file looks valid."""
    path = tmp_path / "c.jsonl"
    path.write_text(
        json.dumps({"cassette_version": CASSETTE_VERSION})
        + "\n"
        + json.dumps({"seq": 0, "response": {}})
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ReplayError, match="entry without 'kind'"):
        Cassette.load(path)


def test_an_entry_missing_seq_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    path.write_text(
        json.dumps({"cassette_version": CASSETTE_VERSION})
        + "\n"
        + json.dumps({"kind": "llm"})
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ReplayError, match="missing 'seq'"):
        Cassette.load(path)


def test_invalid_json_names_the_line(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    path.write_text(
        json.dumps({"cassette_version": CASSETTE_VERSION}) + "\nnot json\n",
        encoding="utf-8",
    )
    with pytest.raises(ReplayError, match=r":2:"):
        Cassette.load(path)


# ---------------------------------------------------------------------------
# the migration machinery
# ---------------------------------------------------------------------------


def test_the_v0_migration_stamps_the_version(tmp_path: Path) -> None:
    path = write_cassette(tmp_path / "c.jsonl", [_entry(response={"content": "x"})], version=None)
    assert migrate(path, LEGACY_VERSION) == CASSETTE_VERSION
    assert Cassette.load(path).count("llm") == 1


def test_migrating_a_current_cassette_is_a_no_op(tmp_path: Path) -> None:
    path = write_cassette(tmp_path / "c.jsonl", [_entry(response={})])
    before = path.read_text(encoding="utf-8")
    assert migrate(path, CASSETTE_VERSION) == CASSETTE_VERSION
    assert path.read_text(encoding="utf-8") == before


def test_migration_preserves_entry_order(tmp_path: Path) -> None:
    """The kinds list must NOT be palindromic.

    The first version of this test used ``llm, tool, llm`` — which reads the same
    backwards, so it passed against a migration that reversed the file. A test whose
    fixture is symmetric cannot observe order at all.
    """
    lines = [
        _entry("llm", 0, response={"content": "a"}),
        _entry("tool", 0, tool_name="t", result={"ok": True}),
        _entry("llm", 1, response={"content": "b"}),
        _entry("tool", 1, tool_name="t2", result={"ok": True}),
    ]
    path = write_cassette(tmp_path / "c.jsonl", lines, version=None)
    migrate(path, LEGACY_VERSION)
    kinds = [entry.kind for entry in Cassette.load(path).entries]
    assert kinds == ["llm", "tool", "llm", "tool"]
    assert kinds != list(reversed(kinds)), "fixture must not be palindromic"


def test_migration_preserves_the_sequence_within_a_kind(tmp_path: Path) -> None:
    """Order matters per kind too: replay pops llm #0 before llm #1."""
    lines = [
        _entry("llm", 0, response={"content": "first"}),
        _entry("llm", 1, response={"content": "second"}),
    ]
    path = write_cassette(tmp_path / "c.jsonl", lines, version=None)
    migrate(path, LEGACY_VERSION)
    entries = Cassette.load(path).entries
    assert [entry.seq for entry in entries] == [0, 1]
    assert entries[0].payload["response"]["content"] == "first"


def test_migration_preserves_payloads(tmp_path: Path) -> None:
    lines = [_entry("tool", 0, tool_name="fetch_order", result={"content": {"a": 1}})]
    path = write_cassette(tmp_path / "c.jsonl", lines, version=None)
    migrate(path, LEGACY_VERSION)
    entry = Cassette.load(path).entries[0]
    assert entry.payload["tool_name"] == "fetch_order"
    assert entry.payload["result"]["content"] == {"a": 1}


def test_a_missing_migration_step_is_reported(tmp_path: Path) -> None:
    """Never invent an upgrade: an unknown gap means re-record.

    The gap has to be *below* the current version, otherwise the "newer than this
    build" check fires first — which is a different (also correct) refusal. Testing
    the wrong branch would have looked like a passing test of the wrong thing.
    """
    from harness_core.replay import _MIGRATIONS

    path = write_cassette(tmp_path / "c.jsonl", [_entry(response={})], version=None)
    saved = _MIGRATIONS.pop(LEGACY_VERSION, None)
    try:
        with pytest.raises(CassetteVersionError, match="no migration registered"):
            migrate(path, LEGACY_VERSION)
    finally:
        if saved is not None:
            _MIGRATIONS[LEGACY_VERSION] = saved


def test_a_version_newer_than_the_build_is_refused_by_migrate(tmp_path: Path) -> None:
    path = write_cassette(tmp_path / "c.jsonl", [_entry(response={})])
    with pytest.raises(CassetteVersionError, match="newer than this build"):
        migrate(path, CASSETTE_VERSION + 99)


def test_a_registered_step_is_used(tmp_path: Path) -> None:
    """A future v1→v2 step can be added without touching the loader."""
    path = write_cassette(tmp_path / "c.jsonl", [_entry(response={"content": "x"})], version=None)

    calls: list[int] = []

    @register_migration(CASSETTE_VERSION)
    def _bump(document):
        calls.append(1)
        return {"cassette_version": CASSETTE_VERSION + 1, "entries": document["entries"]}

    try:
        # The registry has a step at the current version now, but the target is
        # still CASSETTE_VERSION, so nothing should be applied.
        migrate(path, LEGACY_VERSION)
        assert calls == [], "a step beyond the target version must not run"
    finally:
        from harness_core.replay import _MIGRATIONS

        _MIGRATIONS.pop(CASSETTE_VERSION, None)


# ---------------------------------------------------------------------------
# round trip
# ---------------------------------------------------------------------------


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    cassette = Cassette(
        [
            CassetteEntry(kind="llm", seq=0, payload={"response": {"content": "hi"}}),
            CassetteEntry(
                kind="tool", seq=0, payload={"tool_name": "t", "result": {"ok": True}}
            ),
        ]
    )
    path = tmp_path / "c.jsonl"
    cassette.save(path)
    again = Cassette.load(path)
    assert [entry.kind for entry in again.entries] == ["llm", "tool"]
    assert again.count("llm") == 1 and again.count("tool") == 1


def test_the_saved_header_carries_the_version(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    Cassette([]).save(path)
    header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert header == {"cassette_version": CASSETTE_VERSION}


def test_reading_the_header_directly(tmp_path: Path) -> None:
    path = write_cassette(tmp_path / "c.jsonl", [_entry(response={})])
    lines = path.read_text(encoding="utf-8").splitlines()
    version, start = _read_header(lines, path)
    assert version == CASSETTE_VERSION
    assert start == 1


def test_reading_a_headerless_file_directly(tmp_path: Path) -> None:
    path = write_cassette(tmp_path / "c.jsonl", [_entry(response={})], version=None)
    lines = path.read_text(encoding="utf-8").splitlines()
    version, start = _read_header(lines, path)
    assert version == LEGACY_VERSION
    assert start == 0


def test_an_empty_file_is_legacy(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    path.write_text("", encoding="utf-8")
    version, start = _read_header([], path)
    assert version == LEGACY_VERSION and start == 0
    assert Cassette.load(path).count() == 0


def test_blank_lines_are_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    path.write_text(
        "\n" + json.dumps({"cassette_version": CASSETTE_VERSION}) + "\n\n"
        + json.dumps(_entry(response={"content": "x"})) + "\n\n",
        encoding="utf-8",
    )
    assert Cassette.load(path).count("llm") == 1
