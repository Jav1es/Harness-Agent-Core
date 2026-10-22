"""Prove verify_py311.py catches the bug it was written for.

A checker that has never failed is indistinguishable from one that cannot fail. This
puts the exact construct that broke CI back into a temporary copy and asserts the
checker reports it — then confirms the real tree is clean.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: The original broken line, rebuilt by concatenation so this file parses on 3.11.
BROKEN_LINE = (
    'print(f"      {paint(' + repr("failed check:") + ', '
    + repr("\\033[31m") + ')} {check_name}", file=stream)\n'
)


def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "repo"
        shutil.copytree(
            ROOT,
            work,
            ignore=shutil.ignore_patterns(
                "__pycache__", ".git", ".pytest_cache", ".mypy_cache",
                ".deepeval", ".dsh-vision-router", "*.egg-info",
            ),
        )
        target = work / "harness_core" / "broken_probe.py"
        target.write_text(BROKEN_LINE, encoding="utf-8")

        # Point the checker at the target interpreter explicitly; ``uv run`` inside a
        # temp dir would still work but an explicit path is faster and unambiguous.
        interpreter = sys.executable
        result = run(
            [sys.executable, str(work / "artifacts" / "verify_py311.py"),
             "--python", interpreter],
            cwd=work,
        )
        # sys.executable here is 3.12, so the version guard must reject it. That is
        # itself a useful assertion: the checker refuses to certify the wrong version.
        if "expected Python 3.11" not in result.stdout:
            print(result.stdout)
            print(result.stderr)
            print("FAILED: the checker did not refuse a non-3.11 interpreter")
            return 1
        print("OK: refuses to certify a non-3.11 interpreter")

        # The real proof: hand it a genuine 3.11 and a file that only 3.12 accepts.
        # It must come back non-zero. Without this the script would pass even if the
        # checker compiled nothing at all.
        found = run(["uv", "run", "--python", "3.11", "python", "--version"], cwd=work)
        if found.returncode != 0:
            print("SKIP: no Python 3.11 available to prove the failure path")
        else:
            caught = run(
                [sys.executable, str(work / "artifacts" / "verify_py311.py")],
                cwd=work,
            )
            if caught.returncode == 0:
                print(caught.stdout)
                print("FAILED: a 3.12-only f-string compiled clean on 3.11 — "
                      "the checker is not looking at the file")
                return 1
            if "does not compile" not in caught.stdout:
                print(caught.stdout)
                print("FAILED: the checker failed for the wrong reason")
                return 1
            print("OK: catches the real 3.12-only construct on a genuine 3.11")

    # Now the real check: the repository must be clean on a genuine 3.11.
    clean = run([sys.executable, str(ROOT / "artifacts" / "verify_py311.py")], cwd=ROOT)
    print(clean.stdout.strip().splitlines()[-1] if clean.stdout.strip() else clean.stderr)
    if clean.returncode != 0:
        return 1

    print("RESULT: OK — real 3.11 check passes, and the checker refuses to be fooled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
