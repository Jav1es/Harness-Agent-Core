"""Config plumbing for output redaction."""

from __future__ import annotations

import json
from pathlib import Path

from harness_core import cli
from harness_core.config import HarnessConfig
from harness_core.redact import OutputRedactor

CARD = "4111111111111111"

#: ``_build_from_config`` installs the scenario's default business rules
#: (``amount > 0``, ``status != "unknown"``), so a payload must satisfy them before
#: redaction is even reachable. A payload with only ``notes`` would fail validation
#: for reasons unrelated to what these tests are about.
BASE = {"order_id": "ORD-1001", "currency": "CNY", "amount": 1200.0, "status": "settled"}

#: A schema with a free-text field, so a secret has somewhere realistic to appear.
SCHEMA = {
    "type": "object",
    "required": ["order_id", "currency", "amount", "status"],
    "properties": {
        "order_id": {"type": "string"},
        "currency": {"type": "string"},
        "amount": {"type": "number"},
        "status": {"type": "string"},
        "notes": {"type": "string"},
    },
}


def _config(tmp_path: Path, **data) -> Path:
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_redaction_is_off_by_default() -> None:
    """Rewriting a caller's deliverable without being asked is a bad default."""
    assert HarnessConfig().redact is False
    assert HarnessConfig().redactor() is None


def test_redaction_can_be_enabled(tmp_path: Path) -> None:
    config = HarnessConfig.load(str(_config(tmp_path, redact=True)))
    assert config.redact is True
    assert isinstance(config.redactor(), OutputRedactor)


def test_redact_fields_scope_the_walk(tmp_path: Path) -> None:
    config = HarnessConfig.load(str(_config(tmp_path, redact=True, redact_fields=["notes"])))
    redactor = config.redactor()
    assert redactor is not None
    assert redactor.fields == ["notes"]


def test_empty_fields_means_walk_everything(tmp_path: Path) -> None:
    config = HarnessConfig.load(str(_config(tmp_path, redact=True)))
    redactor = config.redactor()
    assert redactor is not None
    assert redactor.fields is None


def test_redact_round_trips_through_to_dict(tmp_path: Path) -> None:
    config = HarnessConfig.load(str(_config(tmp_path, redact=True, redact_fields=["a"])))
    again = HarnessConfig.from_dict(config.to_dict())
    assert again.redact is True
    assert again.redact_fields == ["a"]


def test_the_engine_gets_the_redactor(tmp_path: Path) -> None:
    config = HarnessConfig.load(str(_config(tmp_path, redact=True)))
    engine = cli._build_from_config(config)
    assert engine.redactor is not None


def test_the_engine_gets_no_redactor_by_default(tmp_path: Path) -> None:
    config = HarnessConfig.load(str(_config(tmp_path)))
    engine = cli._build_from_config(config)
    assert engine.redactor is None


def test_a_configured_redactor_actually_masks_a_secret(tmp_path: Path) -> None:
    """Exercise the engine the CLI builds, with a payload we control.

    The CLI's scripted planner emits a fixed payload with no free-text field, so it
    cannot demonstrate redaction end to end — a test that ran ``cli.main`` here
    would pass or fail for reasons unrelated to redaction. Building the same engine
    and feeding it a payload with a secret tests the wiring that matters.
    """
    from harness_core.adapters.llm import FakeLLMAdapter, LLMResponse

    config = HarnessConfig.load(str(_config(tmp_path, redact=True, output_schema=SCHEMA)))
    engine = cli._build_from_config(config)
    engine.llm = FakeLLMAdapter([LLMResponse(content={**BASE, "notes": f"card {CARD}"})])

    result = engine.run("settle")
    assert result.ok, result.failure_reason
    assert CARD not in str(result.execution_result)
    assert "REDACTED" in str(result.execution_result)


def test_without_the_config_flag_the_secret_survives(tmp_path: Path) -> None:
    """Control: the same payload, redaction off, must come through untouched."""
    from harness_core.adapters.llm import FakeLLMAdapter, LLMResponse

    config = HarnessConfig.load(str(_config(tmp_path, output_schema=SCHEMA)))
    engine = cli._build_from_config(config)
    engine.llm = FakeLLMAdapter([LLMResponse(content={**BASE, "notes": f"card {CARD}"})])

    result = engine.run("settle")
    assert result.ok, result.failure_reason
    assert CARD in str(result.execution_result)
