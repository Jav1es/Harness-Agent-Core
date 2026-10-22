"""Prove the package still works when the optional langgraph extra is absent.

The runtime dependency budget is zero. Adding a LangGraph driver must not break
`import harness_core` on a machine that never installed it, so this script hides
`langgraph` / `langchain_core` from the import system and re-checks the basics.

Exit code 0 = the zero-dependency path is intact.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

PROBE = textwrap.dedent(
    """
    import sys

    class Blocker:
        def find_spec(self, name, path=None, target=None):
            root = name.split(".")[0]
            if root in ("langgraph", "langchain_core"):
                raise ImportError("langgraph is intentionally hidden for this probe")
            return None

    sys.meta_path.insert(0, Blocker())

    import harness_core
    from harness_core.loop import DRIVERS, get_driver
    from harness_core.loop_langgraph import langgraph_available

    print("import harness_core: ok")
    print("drivers:", sorted(DRIVERS))
    print("langgraph_available():", langgraph_available())
    print("langgraph registered:", "langgraph" in DRIVERS)
    assert "while" in DRIVERS and "graph" in DRIVERS
    assert not langgraph_available()
    assert "langgraph" not in DRIVERS
    try:
        get_driver("langgraph")
    except ValueError as exc:
        print("get_driver('langgraph') ->", type(exc).__name__)
    else:
        raise AssertionError("expected ValueError for an unavailable driver")
    print("PROBE OK")
    """
)

EVAL = textwrap.dedent(
    """
    import sys

    class Blocker:
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in ("langgraph", "langchain_core"):
                raise ImportError("hidden")
            return None

    sys.meta_path.insert(0, Blocker())
    from harness_core.cli import main
    raise SystemExit(main(["--no-color", "eval"]))
    """
)


def run(code: str, label: str) -> int:
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    print(f"--- {label} (exit {result.returncode}) ---")
    print(result.stdout.strip()[-1200:])
    if result.returncode != 0:
        print(result.stderr.strip()[-1500:])
    return result.returncode


def main() -> int:
    failures = 0
    if run(PROBE, "import + registry without langgraph") != 0:
        failures += 1
    if run(EVAL, "harness-core eval without langgraph") != 0:
        failures += 1
    print("RESULT:", "OK" if failures == 0 else f"{failures} failure(s)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
