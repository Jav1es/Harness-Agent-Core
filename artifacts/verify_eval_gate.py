"""Verify the CI gate turns red: inject a failing scenario, assert exit code 1.

Run with::

    python artifacts/verify_eval_gate.py

It runs the real CLI entry point in a subprocess, so the exit code is the one a CI
job would actually observe.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

INJECT = textwrap.dedent(
    """
    import sys
    from harness_core import cli
    from harness_core.engine import HarnessRunResult
    from harness_core.state import RunStatus
    from harness_core.scenarios import Scenario

    def failing_runner():
        return HarnessRunResult(trace_id="injected", status=RunStatus.FAILED), [
            ("injected failing check", False)
        ]

    cli.SCENARIOS["injected_failure"] = Scenario(
        name="injected_failure",
        description="deliberately broken scenario",
        runner=failing_runner,
    )
    raise SystemExit(cli.main(["--no-color", "eval"]))
    """
)


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def main() -> int:
    failures = 0

    good = run([sys.executable, "-m", "harness_core.cli", "--no-color", "eval"])
    print(f"[gate] baseline eval exit code = {good.returncode} (expected 0)")
    if good.returncode != 0:
        failures += 1
        print(good.stdout[-2000:])
        print(good.stderr[-2000:])

    bad = run([sys.executable, "-c", INJECT])
    print(f"[gate] injected-failure eval exit code = {bad.returncode} (expected 1)")
    if bad.returncode != 1:
        failures += 1
        print(bad.stdout[-2000:])
        print(bad.stderr[-2000:])

    if "injected failing check" not in bad.stdout:
        failures += 1
        print("[gate] FAIL: the report did not name the failing check")
    else:
        print("[gate] report names the failing check")

    # Derive the expected counter from the registry instead of hardcoding it: a
    # hardcoded "7/8" silently rotted the moment a scenario was added, and a gate
    # that fails for the wrong reason trains people to ignore it.
    from harness_core.scenarios import SCENARIOS as REGISTERED

    total = len(REGISTERED) + 1  # +1 for the injected scenario
    expected_counter = f"{total - 1}/{total} scenarios passed"
    if expected_counter in bad.stdout:
        print(f"[gate] pass counter accounts for the injected scenario ({expected_counter})")
    else:
        failures += 1
        print(f"[gate] FAIL: expected {expected_counter!r} in the report")

    print("RESULT:", "OK" if failures == 0 else f"{failures} failure(s)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
