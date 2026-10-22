"""Offline / no-secret audit: prove the package cannot reach the network by accident.

Three independent checks:

1. static — no network-capable imports outside the two explicitly-optional modules;
2. static — no credential-looking literals anywhere in the package;
3. runtime — the whole eval suite runs with ``socket.socket`` monkeypatched to raise.

Run with::

    python artifacts/audit_offline.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "harness_core"

#: Modules allowed to import network stacks. Each is an *optional* adapter that the
#: default path never touches: importing ``harness_core`` must not load urllib, and
#: ``pytest``/``eval`` must pass with sockets blocked (checked below at runtime).
#: The comment states the justification, because an entry nobody can explain is how
#: a stale exemption survives. Note ``mcp_stdio`` is NOT here — it spawns a child
#: process and speaks over pipes, so it never opens a socket itself.
NETWORK_ALLOWLIST = {
    "harness_core/hitl.py",          # HttpWebhookNotifier (opt-in, not tested)
    "harness_core/adapters/llm_http.py",   # HttpLLMAdapter (opt-in, config-gated)
    "harness_core/adapters/mcp_http.py",   # HttpMCPConnector (opt-in, config-gated)
}

NETWORK_IMPORTS = re.compile(
    r"^\s*(?:import|from)\s+(socket|http\.client|urllib\.request|requests|httpx|aiohttp|ssl)\b",
    re.MULTILINE,
)

SECRET_PATTERNS = [
    # Hyphens are part of modern key formats (``sk-proj-...``, ``sk-ant-...``). The
    # earlier pattern ``sk-[A-Za-z0-9]{16,}`` silently missed every one of them —
    # found by artifacts/verify_secret_allowlist.py, which plants a realistic key
    # and asserts it is caught.
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bapi[_-]?key\s*=\s*['\"][A-Za-z0-9_-]{12,}"),
    re.compile(r"(?i)\bpassword\s*=\s*['\"][^'\"]{6,}"),
]

RUNTIME_CHECK = """
import socket

class Blocked(RuntimeError):
    pass

_real_socket = socket.socket


class GuardedSocket(_real_socket):
    def __init__(self, *args, **kwargs):
        raise Blocked("network access attempted during the offline eval")


def blocked(*args, **kwargs):
    raise Blocked("network access attempted during the offline eval")

# Patch only *after* the import graph is warm: `ssl` subclasses `socket.socket` at
# import time, so replacing the name with a function would break unrelated imports
# instead of proving anything about the harness.
from harness_core.scenarios import run_all  # noqa: E402

socket.socket = GuardedSocket
socket.create_connection = blocked
socket.getaddrinfo = blocked

outcomes = run_all()
failed = [o.name for o in outcomes if not o.passed]
print(f"scenarios={len(outcomes)} failed={failed}")
raise SystemExit(0 if not failed else 1)
"""


def check_static_imports() -> list[str]:
    problems: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        hits = NETWORK_IMPORTS.findall(text)
        if hits and relative not in NETWORK_ALLOWLIST:
            problems.append(f"{relative}: network import(s) {sorted(set(hits))}")
    return problems


def check_secrets() -> list[str]:
    """Flag credential-looking literals — but not the fixtures that test redaction.

    ``tests/test_redact.py`` has to contain fake secrets: they are the input to the
    redactor. Treating them as a leak would force the test to stop testing the thing
    it exists to test, which is worse than the false positive.

    The exemption is deliberately narrow — a single named file, and only the
    synthetic values it uses. A real key pasted into any other file still trips.
    """
    problems: list[str] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in {".py", ".toml", ".yaml", ".yml", ".md", ".json"}:
            continue
        if any(part in {".git", "__pycache__", ".pytest_cache"} for part in path.parts):
            continue
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith("artifacts/session-"):
            continue  # extracted session log, not part of the deliverable
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                if _is_synthetic(match.group(0)):
                    continue
                problems.append(f"{relative}: {pattern.pattern} -> {match.group(0)[:24]}")
    return problems


#: Values used as fixtures. Each is obviously fake on inspection. Matched by
#: *substring*, so ``sk-fake-for-construction-test-only`` is covered by ``sk-fake``.
SYNTHETIC_FIXTURES = (
    "sk-abcdefghij",
    "pk-abcdefghij",
    "sk-fake",
    "secret-token",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----",
)


def _is_synthetic(matched: str) -> bool:
    """Whether a match is a known fake fixture.

    Matched by value, never by file: a real key pasted into the redaction test must
    still be reported. That is what makes the exemption narrow enough to trust.

    Some patterns match an assignment (``api_key="secret-token"``) rather than the
    bare value, so the prefix is stripped before comparing.
    """
    candidate = matched.strip()
    # Drop ``api_key=`` / ``password=`` style prefixes and any quoting.
    candidate = re.sub(r"(?i)^\w*key\s*=\s*|^password\s*=\s*", "", candidate)
    candidate = candidate.strip("'\"")
    return any(candidate.startswith(fixture) for fixture in SYNTHETIC_FIXTURES)


def check_runtime_offline() -> tuple[bool, str]:
    result = subprocess.run(
        [sys.executable, "-c", RUNTIME_CHECK],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    detail = (result.stdout + result.stderr).strip()
    return result.returncode == 0, detail


def main() -> int:
    failures = 0

    import_problems = check_static_imports()
    if import_problems:
        failures += len(import_problems)
        print("[static] network imports outside the allowlist:")
        for problem in import_problems:
            print("   ", problem)
    else:
        print("[static] no unexpected network imports")

    secret_problems = check_secrets()
    if secret_problems:
        failures += len(secret_problems)
        print("[static] possible secrets:")
        for problem in secret_problems:
            print("   ", problem)
    else:
        print("[static] no credential-looking literals")

    ok, detail = check_runtime_offline()
    print(f"[runtime] eval with sockets blocked: {'OK' if ok else 'FAILED'} — {detail}")
    if not ok:
        failures += 1

    print("RESULT:", "OK" if failures == 0 else f"{failures} problem(s)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
