"""CLI tests for `replay` (and the record flag that feeds it)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness_core import cli
from harness_core.replay import Cassette


def _record(tmp_path: Path) -> Path:
    target = tmp_path / "cassette.jsonl"
    code = cli.main(["run", "Settle order ORD-1001", "--record", str(target)])
    assert code == 0
    assert target.exists()
    return target


# ---------------------------------------------------------------------------
# record
# ---------------------------------------------------------------------------


def test_run_record_writes_a_cassette(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = _record(tmp_path)
    out = capsys.readouterr().out
    assert "cassette :" in out

    cassette = Cassette.load(target)
    assert cassette.count("llm") == 2
    assert cassette.count("tool") == 1


def test_recorded_cassette_names_the_tool(tmp_path: Path) -> None:
    cassette = Cassette.load(_record(tmp_path))
    tools = [e.payload.get("tool_name") for e in cassette.entries if e.kind == "tool"]
    assert tools == ["fetch_order"]


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


def test_replay_inspect_reports_the_contents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = _record(tmp_path)
    capsys.readouterr()
    assert cli.main(["replay", str(target), "--inspect"]) == 0
    out = capsys.readouterr().out
    assert "llm calls : 2" in out
    assert "tool calls: 1" in out
    assert "fetch_order" in out


def test_replay_inspect_needs_no_task(tmp_path: Path) -> None:
    assert cli.main(["replay", str(_record(tmp_path)), "--inspect"]) == 0


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


def test_replay_reproduces_the_outcome(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = _record(tmp_path)
    capsys.readouterr()
    code = cli.main(
        ["replay", str(target), "--task", "Settle order ORD-1001", "--expect-status", "FIRST_PASS"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "[PASS] FIRST_PASS" in out


def test_replay_fails_on_a_wrong_expectation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = _record(tmp_path)
    capsys.readouterr()
    code = cli.main(
        ["replay", str(target), "--task", "Settle order", "--expect-status", "SELF_HEALED"]
    )
    assert code == 1
    assert "[FAIL] SELF_HEALED" in capsys.readouterr().out


def test_replay_requires_a_task_unless_inspecting(tmp_path: Path) -> None:
    target = _record(tmp_path)
    assert cli.main(["replay", str(target)]) == 2


def test_replay_missing_cassette_exits_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["replay", str(tmp_path / "nope.jsonl"), "--task", "x"]) == 2
    assert "replay error" in capsys.readouterr().err


def test_replay_of_a_truncated_cassette_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A broken fixture must be reported as a replay error, not as a task failure."""
    path = tmp_path / "truncated.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"cassette_version": 1}),
                json.dumps(
                    {
                        "kind": "llm",
                        "seq": 0,
                        "response": {
                            "content": None,
                            "tool_call": {
                                "tool_name": "fetch_order",
                                "arguments": {"order_id": "ORD-1001"},
                            },
                            "raw_text": "",
                            "tokens_estimate": 1,
                            "model": "x",
                            "error": None,
                        },
                    }
                ),
                # no "tool" entry: the run will ask for one
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    code = cli.main(["replay", str(path), "--task", "Settle order"])
    assert code == 1
    assert "replay error" in capsys.readouterr().err


def test_replay_accepts_the_graph_driver(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = _record(tmp_path)
    capsys.readouterr()
    code = cli.main(
        ["replay", str(target), "--task", "Settle order", "--driver", "graph",
         "--expect-status", "FIRST_PASS"]
    )
    assert code == 0
    assert "driver   : graph" in capsys.readouterr().out


def test_replay_honours_the_config_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A replay must validate against the *configured* contract, not a lax default.

    This is the regression guard for a real bug: ``cmd_replay`` used to build a fresh
    ``build_output_guard()`` and drop everything ``--config`` said, so a replay could
    pass under a laxer contract than the run it was supposed to reproduce.

    The cassette is recorded with ``max_retries=1`` so it contains exactly the turns a
    single failed attempt needs. Replaying under a tightened schema must then consume
    those turns and end in FAILED_WITH_CIRCUIT_BREAKER — a laxer guard would have
    accepted the payload and stopped after one turn.
    """
    target = tmp_path / "one_retry.jsonl"
    assert (
        cli.main(
            ["run", "Settle order ORD-1001", "--record", str(target), "--max-retries", "1"]
        )
        == 0
    )
    config = tmp_path / "strict.json"
    config.write_text(
        json.dumps(
            {
                "max_retries": 1,
                "output_schema": {
                    "type": "object",
                    "required": ["order_id", "currency", "amount", "status", "impossible"],
                },
            }
        ),
        encoding="utf-8",
    )
    capsys.readouterr()
    code = cli.main(
        ["replay", str(target), "--task", "Settle order", "--config", str(config),
         "--expect-status", "FAILED_WITH_CIRCUIT_BREAKER"]
    )
    assert code == 0, "the tightened schema must be applied during replay"
    assert "impossible" in capsys.readouterr().out


def test_replay_without_config_uses_the_default_contract(tmp_path: Path) -> None:
    target = _record(tmp_path)
    assert cli.main(["replay", str(target), "--task", "Settle order"]) == 0


def test_expect_status_decides_the_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--expect-status FAILED_*` must be able to exit 0 when the expectation holds.

    Otherwise the flag is unusable for the one thing it exists for: asserting that a
    replay still fails the way it did when it was recorded.
    """
    target = tmp_path / "one_retry.jsonl"
    assert (
        cli.main(
            ["run", "Settle order ORD-1001", "--record", str(target), "--max-retries", "1"]
        )
        == 0
    )
    config = tmp_path / "strict.json"
    config.write_text(
        json.dumps(
            {
                "max_retries": 1,
                "output_schema": {
                    "type": "object",
                    "required": ["order_id", "currency", "amount", "status", "impossible"],
                },
            }
        ),
        encoding="utf-8",
    )
    capsys.readouterr()
    code = cli.main(
        ["replay", str(target), "--task", "Settle order", "--config", str(config),
         "--expect-status", "FAILED_WITH_CIRCUIT_BREAKER"]
    )
    assert code == 0
    assert "[PASS] FAILED_WITH_CIRCUIT_BREAKER" in capsys.readouterr().out


def test_replay_without_expect_status_returns_the_run_exit_code(tmp_path: Path) -> None:
    """No expectation declared ⇒ the exit code still reflects the run itself."""
    target = _record(tmp_path)
    assert cli.main(["replay", str(target), "--task", "Settle order"]) == 0
