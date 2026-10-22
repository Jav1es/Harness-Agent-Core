"""Guardrail tests: whitelist, argument schemas, content filters, business rules."""

from __future__ import annotations

import pytest

from harness_core.errors import (
    ERROR_OUTPUT_FORBIDDEN,
    ERROR_OUTPUT_RULE,
    ERROR_OUTPUT_SCHEMA,
    ERROR_TOOL_NOT_ALLOWED,
    ERROR_TOOL_SCHEMA,
    Finding,
)
from harness_core.guards import (
    BusinessRule,
    OutputGuard,
    ToolGuard,
    ToolSpec,
    validate_json_schema,
)


# ---------------------------------------------------------------------------
# whitelist
# ---------------------------------------------------------------------------


def test_whitelist_rejects_unknown_tool() -> None:
    guard = ToolGuard(whitelist=["fetch_order"])
    findings = guard.check("drop_database", {"db": "orders"})
    assert len(findings) == 1
    assert findings[0].failed_gate == ERROR_TOOL_NOT_ALLOWED
    assert "drop_database" in findings[0].message
    # The hint must be actionable: it names the tools that *are* allowed.
    assert "fetch_order" in findings[0].hint


def test_whitelist_accepts_known_tool() -> None:
    guard = ToolGuard(whitelist=["fetch_order"])
    assert guard.check("fetch_order", {"order_id": "ORD-1"}) == []


def test_whitelist_is_exact_match_not_prefix() -> None:
    guard = ToolGuard(whitelist=["fetch_order"])
    assert guard.check("fetch_order_v2", {}) != []


def test_high_risk_classification_from_flag_and_spec() -> None:
    guard = ToolGuard(
        whitelist=["write_api", "fetch_order"],
        specs={"write_api": ToolSpec("write_api", high_risk=True)},
        high_risk_tools={"fetch_order"},
    )
    assert guard.is_high_risk("write_api") is True
    assert guard.is_high_risk("fetch_order") is True
    assert guard.is_high_risk("unknown") is False


# ---------------------------------------------------------------------------
# argument schema
# ---------------------------------------------------------------------------


def test_tool_argument_schema_rejects_missing_required() -> None:
    guard = ToolGuard(
        whitelist=["fetch_order"],
        specs={
            "fetch_order": ToolSpec(
                "fetch_order",
                input_schema={
                    "type": "object",
                    "required": ["order_id"],
                    "properties": {"order_id": {"type": "string"}},
                },
            )
        },
    )
    findings = guard.check("fetch_order", {"order": "ORD-1"})
    assert findings
    assert findings[0].failed_gate == f"{ERROR_TOOL_SCHEMA}.required"
    assert findings[0].field == "$.order_id"


def test_tool_argument_schema_rejects_wrong_type() -> None:
    guard = ToolGuard(
        whitelist=["compute"],
        specs={
            "compute": ToolSpec(
                "compute",
                input_schema={
                    "type": "object",
                    "properties": {"subtotal": {"type": "number"}},
                },
            )
        },
    )
    findings = guard.check("compute", {"subtotal": "1200"})
    assert findings[0].failed_gate == f"{ERROR_TOOL_SCHEMA}.type"


def test_tool_argument_schema_rejects_extra_fields() -> None:
    guard = ToolGuard(
        whitelist=["fetch_order"],
        specs={
            "fetch_order": ToolSpec(
                "fetch_order",
                input_schema={
                    "type": "object",
                    "properties": {"order_id": {"type": "string"}},
                    "additionalProperties": False,
                },
            )
        },
    )
    findings = guard.check("fetch_order", {"order_id": "ORD-1", "admin": True})
    assert any(f.failed_gate.endswith(".additional") for f in findings)


def test_non_object_arguments_are_rejected() -> None:
    guard = ToolGuard(whitelist=["fetch_order"])
    findings = guard.check("fetch_order", ["ORD-1"])  # type: ignore[arg-type]
    assert findings[0].failed_gate == ERROR_TOOL_SCHEMA


# ---------------------------------------------------------------------------
# JSON-schema subset
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "schema", "expected_gate"),
    [
        ({"a": 1}, {"type": "object", "required": ["b"]}, f"{ERROR_OUTPUT_SCHEMA}.required"),
        ({"a": "x"}, {"type": "object", "properties": {"a": {"type": "integer"}}},
         f"{ERROR_OUTPUT_SCHEMA}.type"),
        ({"a": 5}, {"type": "object", "properties": {"a": {"type": "integer", "minimum": 10}}},
         f"{ERROR_OUTPUT_SCHEMA}.minimum"),
        ({"a": 50}, {"type": "object", "properties": {"a": {"type": "integer", "maximum": 10}}},
         f"{ERROR_OUTPUT_SCHEMA}.maximum"),
        ({"a": "no"}, {"type": "object", "properties": {"a": {"enum": ["yes"]}}},
         f"{ERROR_OUTPUT_SCHEMA}.enum"),
        ({"a": "abc"}, {"type": "object", "properties": {"a": {"pattern": "^x"}}},
         f"{ERROR_OUTPUT_SCHEMA}.pattern"),
        ({"a": [1]}, {"type": "object", "properties": {"a": {"type": "array", "minItems": 2}}},
         f"{ERROR_OUTPUT_SCHEMA}.minItems"),
    ],
)
def test_json_schema_subset(payload: dict, schema: dict, expected_gate: str) -> None:
    findings = validate_json_schema(payload, schema)
    assert findings, f"expected a finding for gate {expected_gate}"
    assert findings[0].failed_gate == expected_gate


def test_json_schema_accepts_valid_payload() -> None:
    schema = {
        "type": "object",
        "required": ["a"],
        "properties": {"a": {"type": "integer", "minimum": 1, "maximum": 10}},
    }
    assert validate_json_schema({"a": 5}, schema) == []


def test_json_schema_bool_is_not_an_integer() -> None:
    findings = validate_json_schema(True, {"type": "integer"})
    assert findings, "bool must not satisfy 'integer' (JSON Schema semantics)"


def test_json_schema_reports_nested_paths() -> None:
    schema = {
        "type": "object",
        "properties": {"order": {"type": "object", "required": ["id"]}},
    }
    findings = validate_json_schema({"order": {}}, schema)
    assert findings[0].field == "$.order.id"


# ---------------------------------------------------------------------------
# forbidden content
# ---------------------------------------------------------------------------


def test_forbidden_string_is_blocked() -> None:
    guard = OutputGuard(forbidden_strings=["TODO"])
    findings = guard.check({"note": "TODO: finish this"})
    assert findings[0].failed_gate == ERROR_OUTPUT_FORBIDDEN
    assert "TODO" in findings[0].message


def test_forbidden_string_scans_nested_structures() -> None:
    guard = OutputGuard(forbidden_strings=["secret"])
    findings = guard.check({"outer": {"inner": ["a", "the secret value"]}})
    assert findings, "content filters must scan nested payloads"


def test_forbidden_regex_is_blocked() -> None:
    guard = OutputGuard(forbidden_patterns=[r"sk-[A-Za-z0-9]{8,}"])
    findings = guard.check({"key": "sk-abcdefgh12345"})
    assert findings[0].failed_gate == ERROR_OUTPUT_FORBIDDEN


def test_invalid_regex_is_a_config_error_not_a_crash() -> None:
    guard = OutputGuard(forbidden_patterns=["(unclosed"])
    findings = guard.check({"a": 1})
    assert findings
    assert findings[0].failed_gate.endswith(".config_error")


def test_clean_payload_passes_content_filters() -> None:
    guard = OutputGuard(forbidden_strings=["TODO"], forbidden_patterns=[r"sk-\w+"])
    assert guard.check({"note": "all good"}) == []


# ---------------------------------------------------------------------------
# business rules
# ---------------------------------------------------------------------------


def test_business_rule_gt_fails() -> None:
    guard = OutputGuard(
        rules=[BusinessRule(name="amount_positive", op="gt", field="amount", value=0)]
    )
    findings = guard.check({"amount": 0})
    assert findings[0].failed_gate == f"{ERROR_OUTPUT_RULE}.amount_positive"


def test_business_rule_dotted_field() -> None:
    guard = OutputGuard(
        rules=[BusinessRule(name="limit", op="le", field="order.amount", value=100)]
    )
    assert guard.check({"order": {"amount": 50}}) == []
    assert guard.check({"order": {"amount": 500}}) != []


def test_business_rule_missing_field_fails() -> None:
    guard = OutputGuard(
        rules=[BusinessRule(name="required_note", op="required", field="note")]
    )
    findings = guard.check({})
    assert findings and findings[0].failed_gate.endswith("required_note")


def test_business_rule_exception_becomes_a_finding() -> None:
    guard = OutputGuard(
        rules=[BusinessRule(name="weird", op="gt", field="amount", value=10)]
    )
    findings = guard.check({"amount": "not-a-number"})
    assert findings, "a rule that cannot evaluate must not silently pass"


def test_warn_severity_rule_does_not_block() -> None:
    guard = OutputGuard(
        rules=[BusinessRule(name="style", op="eq", field="style", value="ok", severity="WARN")]
    )
    findings = guard.check({"style": "meh"})
    assert findings and findings[0].severity == "WARN"


# ---------------------------------------------------------------------------
# fingerprint stability
# ---------------------------------------------------------------------------


def test_fingerprint_is_stable_across_messages() -> None:
    first = Finding(failed_gate="x", message="boom at 12:00")
    second = Finding(failed_gate="x", message="boom at 12:01")
    assert first.fingerprint == second.fingerprint


def test_fingerprint_differs_by_gate() -> None:
    assert Finding(failed_gate="a", message="m").fingerprint != Finding(
        failed_gate="b", message="m"
    ).fingerprint


def test_fingerprint_differs_by_severity() -> None:
    assert Finding(failed_gate="a", message="m", severity="BLOCK").fingerprint != Finding(
        failed_gate="a", message="m", severity="WARN"
    ).fingerprint
