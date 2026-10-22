"""Verify the stdio MCP connector against a real subprocess.

Not part of `tests/` because it is an end-to-end proof of the transport rather than
a unit test: it spawns `tests/mcp_server_stub.py`, speaks JSON-RPC to it, and shows
the failure classification (business vs infrastructure) behaving as documented.

Run with::

    python artifacts/verify_mcp_stdio.py
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STUB = ROOT / "tests" / "mcp_server_stub.py"

PROBE = textwrap.dedent(
    f"""
    import json
    import sys

    sys.path.insert(0, {str(ROOT)!r})

    from harness_core.adapters.mcp import MCPConnector
    from harness_core.adapters.mcp_stdio import (
        MCPTimeoutError,
        MCPTransportError,
        StdioMCPConnector,
    )
    from harness_core.errors import HarnessInfrastructureError

    STUB = {str(STUB)!r}
    failures = 0

    def check(label, condition, detail=""):
        global failures
        if condition:
            print(f"[ok]   {{label}}")
        else:
            failures += 1
            print(f"[FAIL] {{label}} {{detail}}")

    # 1. handshake + discovery
    with StdioMCPConnector(sys.executable, [STUB], timeout=15) as conn:
        check("connector satisfies the MCPConnector protocol", isinstance(conn, MCPConnector))
        tools = conn.list_tools()
        check("lists 4 tools", [t.name for t in tools] == ["echo", "add", "fail", "boom"],
              [t.name for t in tools])
        check("tool schema survives the wire",
              next(t for t in tools if t.name == "add")["input_schema"] if False else
              next(t for t in tools if t.name == "add").input_schema["required"] == ["a", "b"])

        # 2. a successful call
        result = conn.call_tool("echo", {{"text": "hello"}})
        check("echo returns ok=True", result.ok)
        check("text content is flattened", result.content == "hello", repr(result.content))

        # 3. a tool-level failure: normal outcome, no exception
        failed = conn.call_tool("fail", {{}})
        check("isError=true -> ok=False (not an exception)", failed.ok is False)
        check("error text is preserved", failed.error == "this tool always fails", failed.error)

        # 4. a protocol-level error: raises, and is classified as infrastructure
        try:
            conn.call_tool("boom", {{}})
        except MCPTransportError as exc:
            check("JSON-RPC error raises MCPTransportError", True)
            check("transport errors are infrastructure errors",
                  isinstance(exc, HarnessInfrastructureError))
        else:
            check("JSON-RPC error raises MCPTransportError", False)

        proc = conn._proc

    check("close() reaps the child process", proc is not None and proc.poll() is not None)

    # 5. a server that never answers must time out
    try:
        with StdioMCPConnector(sys.executable, [STUB, "--hang"], timeout=1.0) as conn:
            conn.list_tools()
    except MCPTimeoutError:
        check("unresponsive server times out", True)
    else:
        check("unresponsive server times out", False)

    # 6. a server that cannot start must fail loudly
    try:
        with StdioMCPConnector("definitely-not-a-real-binary-xyz", timeout=2.0) as conn:
            conn.list_tools()
    except MCPTransportError:
        check("missing binary raises MCPTransportError", True)
    else:
        check("missing binary raises MCPTransportError", False)

    print("RESULT:", "OK" if failures == 0 else f"{{failures}} failure(s)")
    raise SystemExit(0 if failures == 0 else 1)
    """
)


def main() -> int:
    import subprocess

    if not STUB.exists():
        print(f"stub server missing: {STUB}")
        return 1
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr.strip()[-2000:])
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
