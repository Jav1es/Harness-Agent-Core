"""CLI tests: run / demo / eval and the exit-code contract (the CI gate)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness_core import cli
from harness_core.scenarios import SCENARIOS, run_all


# ---------------------------------------------------------------------------
# eval — the CI gate
# ---------------------------------------------------------------------------


def test_eval_exits_zero_when_all_scenarios_pass(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["eval"])
    assert code == 0
    out = capsys.readouterr().out
    assert "scenarios passed" in out
    assert "exit code: 0" in out


def test_eval_reports_every_scenario(capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["eval"])
    out = capsys.readouterr().out
    for name in SCENARIOS:
        assert name in out, f"scenario {name} missing from the report"


def test_eval_exits_one_when_a_scenario_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failing scenario must turn the CI gate red."""

    def failing_runner():
        from harness_core.engine import HarnessRunResult
        from harness_core.state import RunStatus

        result = HarnessRunResult(trace_id="t", status=RunStatus.FAILED)
        return result, [("deliberately failing check", False)]

    monkeypatch.setitem(
        cli.SCENARIOS,
        "broken",
        cli.SCENARIOS["self_healing_chain"].__class__(
            name="broken", description="injected failure", runner=failing_runner
        ),
    )
    code = cli.main(["eval"])
    assert code == 1
    out = capsys.readouterr().out
    assert "failed check:" in out


def test_eval_json_report_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["eval", "--json", "-"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["total"] == len(SCENARIOS)
    assert payload["failed"] == 0
    assert len(payload["scenarios"]) == payload["total"]


def test_eval_json_report_to_file(tmp_path: Path) -> None:
    target = tmp_path / "eval.json"
    code = cli.main(["eval", "--json", str(target)])
    assert code == 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["passed"] == payload["total"]


# ---------------------------------------------------------------------------
# demo
# ---------------------------------------------------------------------------


def test_demo_exits_zero_and_prints_the_chain(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["demo"])
    assert code == 0
    out = capsys.readouterr().out
    assert "self_healing_chain" in out
    assert "circuit_breaker_recurred" in out
    assert "[PASS]" in out
    assert "All demo scenarios behaved as specified." in out


def test_demo_writes_a_trace_file(tmp_path: Path) -> None:
    target = tmp_path / "demo.jsonl"
    assert cli.main(["demo", "--trace", str(target)]) == 0
    lines = target.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    for line in lines:
        record = json.loads(line)
        assert {"name", "passed", "status", "trace_id"} <= set(record)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def test_run_succeeds_and_prints_the_trace_id(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["run", "Settle order ORD-1001"])
    assert code == 0
    out = capsys.readouterr().out
    assert "trace_id" in out
    assert "status" in out
    # The scripted planner reads the order first, so the run takes two model turns.
    assert "attempts : 2" in out


def test_run_with_config_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "harness.json"
    config.write_text(
        json.dumps({"max_retries": 2, "tool_whitelist": ["fetch_order"]}), encoding="utf-8"
    )
    assert cli.main(["run", "Settle order", "--config", str(config)]) == 0
    assert "trace_id" in capsys.readouterr().out


def test_run_with_bad_config_exits_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    assert cli.main(["run", "task", "--config", str(bad)]) == 2
    assert "config error" in capsys.readouterr().err


def test_run_writes_a_jsonl_trace(tmp_path: Path) -> None:
    target = tmp_path / "run.jsonl"
    assert cli.main(["run", "Settle order", "--trace", str(target)]) == 0
    record = json.loads(target.read_text(encoding="utf-8").strip())
    assert record["trace_id"]
    assert record["spans"]


def test_run_accepts_both_drivers(capsys: pytest.CaptureFixture[str]) -> None:
    for driver in ("while", "graph"):
        assert cli.main(["run", "Settle order", "--driver", driver]) == 0
        assert f"driver   : {driver}" in capsys.readouterr().out


def test_run_rejects_an_unknown_driver() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["run", "Settle order", "--driver", "temporal"])
    assert excinfo.value.code == 2


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    assert "harness-core" in capsys.readouterr().out


def test_unknown_command_exits_two() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["nonsense"])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# scenario suite itself
# ---------------------------------------------------------------------------


def test_all_builtin_scenarios_pass() -> None:
    outcomes = run_all()
    failed = [outcome.name for outcome in outcomes if not outcome.passed]
    assert failed == [], f"failing scenarios: {failed}"


def test_every_scenario_asserts_something() -> None:
    for outcome in run_all():
        assert outcome.checks, f"{outcome.name} asserted nothing"


def test_scenario_statuses_are_the_expected_ones() -> None:
    expected = {
        "self_healing_chain": "SELF_HEALED",
        "circuit_breaker_exhausted": "FAILED_WITH_CIRCUIT_BREAKER",
        "circuit_breaker_recurred": "FAILED_WITH_CIRCUIT_BREAKER",
        "hitl_approve": "FIRST_PASS",
        "hitl_reject": "REJECTED_BY_HUMAN",
        "warnings_do_not_retry": "OK_WITH_WARNINGS",
    }
    actual = {outcome.name: outcome.status for outcome in run_all()}
    for name, status in expected.items():
        assert actual[name] == status, f"{name}: expected {status}, got {actual[name]}"
