"""Prove the streaming path is actually faster than reading to EOF.

The unit tests assert `elapsed < 3s` against a 5s hold. That is a real assertion,
but it does not prove the *old* code would fail it. This script runs both
implementations against the same stub and prints the numbers side by side.

A test that passes on both the fixed and the broken code proves nothing.
"""

import json
import subprocess
import sys
import time
import urllib.request

STUB = "tests/mcp_http_server_stub.py"
HOLD = 5.0


def start(*flags):
    p = subprocess.Popen(
        [sys.executable, STUB, *flags],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    assert p.stdout is not None
    while True:
        line = p.stdout.readline()
        if line.startswith("PORT "):
            return p, int(line.split()[1])
        if not line:
            raise RuntimeError("no port")


def old_style(url: str) -> float:
    """Read the whole body, exactly as the pre-streaming client did."""
    req = urllib.request.Request(
        url,
        data=b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(req, timeout=HOLD * 3) as r:
        _ = r.read()  # <-- waits for EOF
    return time.perf_counter() - started


def new_style(url: str) -> float:
    """Use the real streaming client."""
    from harness_core.adapters.mcp_http import HttpMCPConnector

    conn = HttpMCPConnector(url, timeout=HOLD * 3)
    try:
        started = time.perf_counter()
        conn.initialize()
        return time.perf_counter() - started
    finally:
        conn.close()


p, port = start("--sse", "--hold", str(HOLD))
url = f"http://127.0.0.1:{port}/mcp"
try:
    old = old_style(url)
    new = new_style(url)
finally:
    p.terminate()
    p.wait(timeout=5)

print(f"hold                : {HOLD:.1f}s")
print(f"read-to-EOF (old)   : {old:.2f}s")
print(f"streaming    (new)  : {new:.2f}s")
print(f"speedup             : {old / new:.1f}x")

assert new < 3.0, "streaming client should return well before the hold expires"
assert old > 4.0, "read-to-EOF must actually wait — otherwise the stub is not holding"
print("RESULT: OK — the timing test distinguishes the two implementations")
