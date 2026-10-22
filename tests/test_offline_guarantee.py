"""The package must stay importable and runnable without any network stack.

The allowlist in ``artifacts/audit_offline.py`` says which modules *may* import
urllib. That is a static check and it has an obvious weakness: an allowlist can be
widened until it says nothing. This test closes the gap from the other side by
asserting the *default* path never loads the network stack at all.

Both directions matter:

* the static check catches a new module that imports urllib without being reviewed;
* this test catches the case where a reviewed module gets imported eagerly by
  ``__init__`` and quietly drags urllib into every run.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def test_importing_the_package_does_not_load_urllib() -> None:
    result = _run(
        "import sys, harness_core\n"
        "print('urllib.request' in sys.modules)\n"
        "print('socket' in sys.modules)\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["False", "False"], result.stdout


def test_the_http_adapter_is_not_imported_eagerly() -> None:
    result = _run(
        "import sys, harness_core\n"
        "print('harness_core.adapters.llm_http' in sys.modules)\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_the_engine_imports_without_the_network_stack() -> None:
    """The core path — engine, guards, breaker, loop — must be network-free."""
    result = _run(
        "import sys\n"
        "from harness_core.engine import HarnessOrchestrationEngine\n"
        "from harness_core import scenarios\n"
        "print('urllib.request' in sys.modules)\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_eval_runs_with_sockets_blocked() -> None:
    """Belt and braces: block sockets in-process and run the real eval."""
    code = (
        "import socket\n"
        "class Blocked(socket.socket):\n"
        "    def __init__(self, *a, **k):\n"
        "        raise RuntimeError('network disabled')\n"
        "socket.socket = Blocked\n"
        "from harness_core.cli import main\n"
        "raise SystemExit(main(['--no-color', 'eval']))\n"
    )
    result = _run(code)
    assert result.returncode == 0, (result.stdout + result.stderr)[-2000:]
    assert "scenarios passed" in (result.stdout + result.stderr)
