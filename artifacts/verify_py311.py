"""Compile every source file with a real Python 3.11 interpreter.

Why not ``ast.parse(feature_version=(3, 11))``
-----------------------------------------------
That was the first attempt and it was **wrong**. ``feature_version`` gates grammar
*features* (walrus, match, parenthesised context managers); it does not roll back
the parser's tolerance. PEP 701 relaxed f-strings in 3.12 — a backslash inside an
f-string expression part — and on a 3.12 interpreter ``ast.parse`` accepts it even
with ``feature_version=(3, 11)``. Measured:

    running python (3, 12)
      feature_version=(3, 11): PARSED (no error)   <- the bug walks straight through
      compile(): OK

A checker that reports "OK" on the exact construct that broke CI is worse than no
checker: it manufactures confidence. So this script uses a real interpreter.

Usage
-----
    python artifacts/verify_py311.py
    python artifacts/verify_py311.py --python C:\\path\\to\\python3.11.exe

Exit code 0 only if every file compiles on the target interpreter.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TARGET_MINOR = "3.11"

#: Directories that hold Python source belonging to the deliverable.
SOURCE_DIRS = ("harness_core", "tests", "artifacts", "examples")

#: Copied into the target interpreter. Deliberately tiny and dependency-free.
COMPILE_SCRIPT = """
import compileall
import sys

ok = True
for directory in sys.argv[1:]:
    if not compileall.compile_dir(directory, quiet=2):
        ok = False
sys.exit(0 if ok else 1)
"""


def find_python(explicit: str | None) -> list[str]:
    """Resolve a command that runs the target interpreter."""
    if explicit:
        return [explicit]
    # ``uv run`` can fetch the interpreter on demand; fall back to a bare name if uv
    # is absent so the script still works on a machine that installed 3.11 directly.
    if shutil.which("uv"):
        return ["uv", "run", "--python", TARGET_MINOR, "python"]
    return [f"python{TARGET_MINOR}"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile sources on Python 3.11")
    parser.add_argument("--python", default=None, help="path to a Python 3.11 executable")
    args = parser.parse_args(argv)

    command = find_python(args.python)
    directories = [str(ROOT / name) for name in SOURCE_DIRS if (ROOT / name).exists()]

    print(f"[3.11] interpreter: {' '.join(command)}")
    version = subprocess.run(
        [*command, "--version"], capture_output=True, text=True, encoding="utf-8"
    )
    if version.returncode != 0:
        print(f"[3.11] cannot run the interpreter: {version.stderr.strip()}")
        return 1
    reported = version.stdout.strip()
    print(f"[3.11] reported: {reported}")
    if TARGET_MINOR not in reported:
        # Refuse to certify a different interpreter: the whole point is the version.
        print(f"[3.11] FAIL: expected Python {TARGET_MINOR}, got {reported}")
        return 1

    result = subprocess.run(
        [*command, "-c", COMPILE_SCRIPT, *directories],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode != 0:
        if result.stderr.strip():
            print(result.stderr.strip())
        print(f"[3.11] RESULT: FAIL — the source does not compile on {TARGET_MINOR}")
        return 1

    print(f"[3.11] RESULT: OK — every source file compiles on {TARGET_MINOR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
