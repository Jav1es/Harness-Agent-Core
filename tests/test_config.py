"""Config tests: JSON / YAML loading, env overrides, precedence, error handling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness_core.config import ConfigError, HarnessConfig
from harness_core.guards import BusinessRule


def test_defaults() -> None:
    config = HarnessConfig()
    assert config.max_retries == 3
    assert config.repeat_threshold == 1
    assert config.tool_whitelist == []
    assert config.high_risk_tools == []


def test_load_json(tmp_path: Path) -> None:
    path = tmp_path / "harness.json"
    path.write_text(
        json.dumps(
            {
                "max_retries": 5,
                "tool_whitelist": ["fetch_order"],
                "high_risk_tools": ["write_api"],
                "forbidden_strings": ["TODO"],
                "forbidden_patterns": [r"sk-\w+"],
                "output_schema": {"type": "object"},
                "rules": [
                    {"name": "amount_positive", "op": "gt", "field": "amount", "value": 0}
                ],
                "tools": {
                    "fetch_order": {
                        "description": "read",
                        "input_schema": {"type": "object", "required": ["order_id"]},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    config = HarnessConfig.load(path, env={})
    assert config.max_retries == 5
    assert config.tool_whitelist == ["fetch_order"]
    assert config.high_risk_tools == ["write_api"]
    assert config.forbidden_patterns == [r"sk-\w+"]
    assert config.rules[0].name == "amount_positive"
    assert config.tools["fetch_order"].input_schema["required"] == ["order_id"]


def test_load_yaml(tmp_path: Path) -> None:
    pytest.importorskip("yaml")
    path = tmp_path / "harness.yaml"
    path.write_text(
        "max_retries: 4\n"
        "tool_whitelist:\n  - fetch_order\n"
        "forbidden_strings:\n  - TODO\n"
        "tools:\n"
        "  fetch_order:\n"
        "    input_schema:\n"
        "      type: object\n"
        "      required: [order_id]\n",
        encoding="utf-8",
    )
    config = HarnessConfig.load(path, env={})
    assert config.max_retries == 4
    assert config.tool_whitelist == ["fetch_order"]
    assert config.tools["fetch_order"].input_schema["required"] == ["order_id"]


def test_tool_high_risk_flag_merges_into_the_high_risk_list(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text(
        json.dumps({"tools": {"write_api": {"high_risk": True}}}), encoding="utf-8"
    )
    config = HarnessConfig.load(path, env={})
    assert "write_api" in config.high_risk_tools


def test_env_overrides_file(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"max_retries": 2, "tool_whitelist": ["a"]}), encoding="utf-8")
    config = HarnessConfig.load(
        path,
        env={
            "HARNESS_MAX_RETRIES": "7",
            "HARNESS_TOOL_WHITELIST": "x, y ,z",
            "HARNESS_VERBOSE": "true",
        },
    )
    assert config.max_retries == 7
    assert config.tool_whitelist == ["x", "y", "z"]
    assert config.verbose is True


def test_env_high_risk_marks_the_tool_spec(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"tools": {"write_api": {}}}), encoding="utf-8")
    config = HarnessConfig.load(path, env={"HARNESS_HIGH_RISK_TOOLS": "write_api"})
    assert config.tools["write_api"].high_risk is True


def test_missing_file_raises() -> None:
    with pytest.raises(ConfigError, match="not found"):
        HarnessConfig.load("does-not-exist.yaml", env={})


def test_invalid_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid JSON"):
        HarnessConfig.load(path, env={})


def test_rule_without_name_raises(tmp_path: Path) -> None:
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"rules": [{"op": "gt"}]}), encoding="utf-8")
    with pytest.raises(ConfigError, match="name"):
        HarnessConfig.load(path, env={})


def test_roundtrip_to_dict_from_dict() -> None:
    original = HarnessConfig(
        max_retries=2,
        tool_whitelist=["a"],
        forbidden_strings=["x"],
        rules=[BusinessRule(name="r", op="gt", field="v", value=1)],
    )
    restored = HarnessConfig.from_dict(original.to_dict())
    assert restored.max_retries == original.max_retries
    assert restored.tool_whitelist == original.tool_whitelist
    assert restored.rules[0].name == "r"


def test_output_guard_built_from_config() -> None:
    config = HarnessConfig(
        output_schema={"type": "object"},
        forbidden_strings=["TODO"],
        forbidden_patterns=[r"\d{16}"],
        rules=[BusinessRule(name="r", op="gt", field="v", value=1)],
    )
    guard = config.output_guard()
    assert guard.output_schema == {"type": "object"}
    assert guard.forbidden_strings == ["TODO"]
    assert len(guard.rules) == 1
