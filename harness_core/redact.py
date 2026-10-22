"""Output-side redaction: change the payload, don't just reject it.

Why this is not another ``forbidden_patterns``
----------------------------------------------
The existing forbidden-string gate is a *rejection* gate: a match becomes a BLOCK
finding and the run is sent back to the model. That is the wrong tool for secrets.

Consider a deliverable that is otherwise perfect but happens to quote a customer's
card number. Rejecting it means:

* the model is asked to "fix" something it cannot see the problem in;
* it will retry with a *paraphrase*, and paraphrases of secrets are still secrets;
* if the retry budget runs out, the run fails — and the operator learns nothing
  about the leak except that the run broke.

Redaction is the honest alternative: the leak is **removed from the payload**, the
event is recorded, and the run continues. A rejection gate protects nothing if the
secret already reached the log line describing the rejection.

Ordering matters
----------------
Redaction runs **before** the deterministic gates, not after. If the card number is
still in the payload when ``forbidden_patterns`` runs, the run blocks on a string
that was about to be removed anyway. Redact first, then validate what is actually
leaving the harness.

What this deliberately does not do
----------------------------------
It does not try to be clever about context. A regex cannot tell a real card number
from a test fixture, so every redactor carries a ``kind`` label and every redaction
is reported with its location. Over-redacting a fixture is visible and fixable;
under-redacting a real secret is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from .errors import Finding

#: Gate prefix for redaction findings. Distinct from ``output.forbidden_pattern``
#: on purpose: "we removed something" and "we rejected the payload" are different
#: events and must not be read as the same thing in a trace.
ERROR_REDACTED = "output.redacted"

#: What a redacted value is replaced with. Contains the kind so a reader can tell
#: *what* was removed without seeing it.
def mask_for(kind: str) -> str:
    return f"[REDACTED:{kind}]"


@dataclass(frozen=True)
class Redactor:
    """One rule: a pattern to find and the label to replace it with."""

    kind: str
    pattern: str
    #: Optional replacement template. ``None`` uses ``[REDACTED:<kind>]``.
    replacement: str | None = None
    #: Optional function that decides whether a specific match is a real hit.
    #: Receives the matched text; returning False leaves the text untouched.
    accept: Any = None

    def to_finding(self, count: int, locations: Sequence[str]) -> Finding:
        where = ", ".join(sorted(set(locations))) or "unknown"
        return Finding(
            failed_gate=f"{ERROR_REDACTED}.{self.kind}",
            message=(
                f"redacted {count} {self.kind} value(s) from the output before it left "
                f"the harness (at: {where})"
            ),
            # WARN, not BLOCK: the leak was handled. Blocking here would report a
            # problem that no longer exists in the payload.
            severity="WARN",
            resolution_hint=(
                "检查模型为什么把这类内容放进产出；必要时在 prompt 里明确禁止复述"
            ),
        )


def _luhn_ok(digits: str) -> bool:
    """Mod-10 check. Cuts the false-positive rate for card numbers enormously."""
    total = 0
    reverse = digits[::-1]
    for index, char in enumerate(reverse):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _card_like(text: str) -> bool:
    digits = re.sub(r"[\s-]", "", text)
    return 13 <= len(digits) <= 19 and digits.isdigit() and _luhn_ok(digits)


#: Default redactors, ordered most specific first.
#:
#: ``api_key`` comes before ``bearer_token`` so a ``sk-`` key is labelled as a key
#: rather than as a generic token — the more specific label is more useful in a
#: trace, and the first matching redactor wins per span.
DEFAULT_REDACTORS: tuple[Redactor, ...] = (
    Redactor("private_key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    Redactor("api_key", r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}\b"),
    Redactor("bearer_token", r"(?i)\bBearer\s+[A-Za-z0-9._\-]{16,}"),
    Redactor("email", r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
    # The separators are captured *between* digits, never trailing: a pattern that
    # ends in an optional space swallows the space after the number and glues the
    # next word onto the mask ("...1111 y" -> "[REDACTED]y").
    Redactor("card_number", r"\b\d{1,4}(?:[ -]?\d{1,4}){2,6}\b", accept=_card_like),
    Redactor("cn_id", r"\b\d{17}[\dXx]\b"),
    Redactor("phone_cn", r"(?<!\d)1[3-9]\d{9}(?!\d)"),
)


@dataclass
class OutputRedactor:
    """Applies redactors to a payload and reports what was removed.

    ``enabled`` exists so a caller can inspect a payload without mutating it (useful
    in tests and in a dry-run mode) without having to construct a different class.
    """

    redactors: Sequence[Redactor] = field(default_factory=lambda: DEFAULT_REDACTORS)
    enabled: bool = True
    #: Free-text fields to walk. ``None`` means "walk the whole payload".
    fields: Sequence[str] | None = None

    def redact(self, payload: Any) -> tuple[Any, list[Finding]]:
        """Return ``(redacted_payload, findings)``. Never raises on a bad pattern."""
        if not self.enabled:
            return payload, []

        counts: dict[str, int] = {}
        locations: dict[str, list[str]] = {}

        def walk(value: Any, path: str) -> Any:
            if isinstance(value, str):
                return self._redact_text(value, path, counts, locations)
            if isinstance(value, dict):
                return {key: walk(item, f"{path}.{key}" if path else str(key))
                        for key, item in value.items()}
            if isinstance(value, list):
                return [walk(item, f"{path}[{index}]") for index, item in enumerate(value)]
            return value

        redacted = walk(payload, "")
        findings = [
            redactor.to_finding(counts[redactor.kind], locations.get(redactor.kind, []))
            for redactor in self.redactors
            if counts.get(redactor.kind)
        ]
        return redacted, findings

    # -------------------------------------------------------------- internals
    def _redact_text(
        self,
        text: str,
        path: str,
        counts: dict[str, int],
        locations: dict[str, list[str]],
    ) -> str:
        result = text
        for redactor in self.redactors:
            if self.fields is not None and not _path_selected(path, self.fields):
                continue
            try:
                pattern = re.compile(redactor.pattern)
            except re.error:
                # A broken pattern must not abort a run: skip it and let the
                # forbidden-pattern gate report the config error separately.
                continue
            replacement = redactor.replacement or mask_for(redactor.kind)
            accept = redactor.accept

            def substitute(match: re.Match[str], _accept: Any = accept, _rep: str = replacement,
                           _kind: str = redactor.kind) -> str:
                matched = match.group(0)
                if _accept is not None and not _accept(matched):
                    return matched
                counts[_kind] = counts.get(_kind, 0) + 1
                locations.setdefault(_kind, []).append(path or "<root>")
                return _rep

            result = pattern.sub(substitute, result)
        return result


def _path_selected(path: str, fields: Sequence[str]) -> bool:
    """Whether ``path`` is one of the configured field names.

    Matches the leaf name, not the whole dotted path: a caller who says
    ``fields=["notes"]`` means "every ``notes`` field", not "the top-level one".
    """
    leaf = path.rsplit(".", 1)[-1]
    leaf = re.sub(r"\[\d+\]$", "", leaf)
    return leaf in fields


def redact(payload: Any, redactor: OutputRedactor | None = None) -> tuple[Any, list[Finding]]:
    """Convenience wrapper: redact with the defaults unless told otherwise."""
    return (redactor or OutputRedactor()).redact(payload)
