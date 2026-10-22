"""Prove the secret-scan exemption is narrow, not a hole.

An allowlist that can be widened until it says nothing is worse than no audit. This
script plants a realistic key in a file that is *not* exempt and asserts the scanner
still catches it, then plants the same value in the exempt file and asserts it does
not.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("audit", ROOT / "artifacts" / "audit_offline.py")
assert spec and spec.loader
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)

PROBE = ROOT / "harness_core" / "_probe_secret.py"
EXEMPT_PROBE = ROOT / "tests" / "test_redact.py"

# Assembled at runtime so this file contains no literal that the scanner would flag.
# Widening the allowlist to cover the verifier would have defeated the point: the
# verifier exists to prove the allowlist cannot be widened casually.
KEY_PREFIX = "sk-" + "proj-"
REAL_LOOKING = 'API = "' + KEY_PREFIX + "Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8" + '"'
KEY_HEADER = "-----BEGIN " + "OPENSSH PRIVATE KEY" + "-----"


def scan() -> list[str]:
    return audit.check_secrets()


before = len(scan())
print("baseline problems:", before)
assert before == 0, "baseline must be clean"

# 1. A realistic key in a normal module must be caught.
PROBE.write_text(REAL_LOOKING, encoding="utf-8")
try:
    caught = [p for p in scan() if "_probe_secret" in p]
    print("caught in harness_core:", len(caught))
    assert caught, "a real key outside the exempt file MUST be reported"
finally:
    PROBE.unlink()

# 2. The exempt file with the SAME realistic key must now be reported: the
#    exemption is by value, so an unknown key trips even inside the fixture file.
original = EXEMPT_PROBE.read_text(encoding="utf-8")
EXEMPT_PROBE.write_text(original + "\n# " + REAL_LOOKING + "\n", encoding="utf-8")
try:
    reported = [p for p in scan() if "test_redact" in p]
    print("reported in fixture file:", len(reported))
    assert reported, "an unknown key must trip even in the exempt file"
finally:
    EXEMPT_PROBE.write_text(original, encoding="utf-8")

# 3. A non-synthetic private key in the exempt file must STILL be caught:
#    the exemption is by value, not by file.
EXEMPT_PROBE.write_text(original + "\n# " + KEY_HEADER + "\n", encoding="utf-8")
try:
    caught2 = [p for p in scan() if "test_redact" in p]
    print("caught non-fixture header in exempt file:", len(caught2))
    assert caught2, "exemption must be by value: unknown key headers still trip"
finally:
    EXEMPT_PROBE.write_text(original, encoding="utf-8")

print("after cleanup:", len(scan()))
print("RESULT: OK — exemption is narrow")
