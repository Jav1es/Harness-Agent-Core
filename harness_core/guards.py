"""Deterministic guardrails: whitelist, argument schemas, content filters, rules.

Every function in this module is a **pure, offline, zero-LLM** check. That is the
whole point of the harness: validation must not be another non-deterministic model
call, otherwise "guarded" means nothing.

Public surface
--------------
* :func:`validate_json_schema` – a small JSON-Schema subset with JSON-Pointer-ish
  error paths (no third-party dependency, so the package installs offline).
* :class:`ToolGuard` – the whitelist / schema / high-risk gate for tool calls.
* :class:`OutputGuard` – forbidden strings, forbidden regexes and business rules
  applied to whatever the model produced.
* :class:`BusinessRule` / :func:`make_rule` – declarative rules loadable from YAML.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from .errors import (
    ERROR_OUTPUT_FORBIDDEN,
    ERROR_OUTPUT_RULE,
    ERROR_OUTPUT_SCHEMA,
    ERROR_TOOL_NOT_ALLOWED,
    ERROR_TOOL_SCHEMA,
    Finding,
    Severity,
    coerce_severity,
)

# ---------------------------------------------------------------------------
# Minimal JSON Schema subset
# ---------------------------------------------------------------------------

_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list, tuple),
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "null": (type(None),),
}


def _json_type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    if value is None:
        return "null"
    return type(value).__name__


def _type_matches(value: Any, expected: str | Sequence[str]) -> bool:
    names = [expected] if isinstance(expected, str) else list(expected)
    for name in names:
        # bool is a subclass of int in Python; JSON Schema treats them apart.
        if name == "boolean" and isinstance(value, bool):
            return True
        if name in {"number", "integer"} and isinstance(value, bool):
            continue
        allowed = _TYPE_MAP.get(name)
        if allowed and isinstance(value, allowed):
            if name == "integer" and isinstance(value, bool):
                continue
            return True
    return False


def validate_json_schema(
    payload: Any,
    schema: dict[str, Any],
    *,
    path: str = "$",
    gate_prefix: str = ERROR_OUTPUT_SCHEMA,
    tool_name: str | None = None,
    severity: Severity = "BLOCK",
) -> list[Finding]:
    """Validate ``payload`` against a JSON-Schema subset.

    Supported keywords: ``type``, ``required``, ``properties``,
    ``additionalProperties``, ``enum``, ``const``, ``minimum``, ``maximum``,
    ``minLength``, ``maxLength``, ``pattern``, ``items``, ``minItems``,
    ``maxItems``. Unknown keywords are ignored on purpose — a harness must not
    reject a valid payload because a schema used a feature it does not know.
    """
    findings: list[Finding] = []
    sev = severity

    expected_type = schema.get("type")
    if expected_type is not None and not _type_matches(payload, expected_type):
        findings.append(
            Finding(
                failed_gate=f"{gate_prefix}.type",
                message=f"{path}: expected {expected_type}, got {_json_type_name(payload)}",
                severity=sev,
                tool_name=tool_name,
                field=path,
                resolution_hint=f"{path} 必须是 {expected_type} 类型",
            )
        )
        return findings  # deeper checks would only produce noise

    if "enum" in schema and payload not in schema["enum"]:
        findings.append(
            Finding(
                failed_gate=f"{gate_prefix}.enum",
                message=f"{path}: {payload!r} not in {schema['enum']}",
                severity=sev,
                tool_name=tool_name,
                field=path,
                resolution_hint=f"{path} 只能取 {schema['enum']} 之一",
            )
        )

    if "const" in schema and payload != schema["const"]:
        findings.append(
            Finding(
                failed_gate=f"{gate_prefix}.const",
                message=f"{path}: expected constant {schema['const']!r}",
                severity=sev,
                tool_name=tool_name,
                field=path,
                resolution_hint=f"{path} 必须等于 {schema['const']!r}",
            )
        )

    if isinstance(payload, dict):
        for key in schema.get("required", []):
            if key not in payload:
                findings.append(
                    Finding(
                        failed_gate=f"{gate_prefix}.required",
                        message=f"{path}.{key}: required field is missing",
                        severity=sev,
                        tool_name=tool_name,
                        # Full JSON path, so the repair prompt can point at the
                        # exact location instead of just the leaf name.
                        field=f"{path}.{key}",
                        resolution_hint=f"补上必填字段 {key}",
                    )
                )
        properties: dict[str, Any] = schema.get("properties", {}) or {}
        for key, subschema in properties.items():
            if key in payload:
                findings.extend(
                    validate_json_schema(
                        payload[key],
                        subschema,
                        path=f"{path}.{key}",
                        gate_prefix=gate_prefix,
                        tool_name=tool_name,
                        severity=severity,
                    )
                )
        additional = schema.get("additionalProperties", True)
        if additional is False:
            for key in payload:
                if key not in properties:
                    findings.append(
                        Finding(
                            failed_gate=f"{gate_prefix}.additional",
                            message=f"{path}.{key}: unexpected field",
                            severity=sev,
                            tool_name=tool_name,
                            field=key,
                            resolution_hint=f"删除未声明字段 {key}（不允许额外字段）",
                        )
                    )

    if isinstance(payload, (int, float)) and not isinstance(payload, bool):
        if "minimum" in schema and payload < schema["minimum"]:
            findings.append(
                Finding(
                    failed_gate=f"{gate_prefix}.minimum",
                    message=f"{path}: {payload} < minimum {schema['minimum']}",
                    severity=sev,
                    tool_name=tool_name,
                    field=path,
                    resolution_hint=f"{path} 不得小于 {schema['minimum']}",
                )
            )
        if "maximum" in schema and payload > schema["maximum"]:
            findings.append(
                Finding(
                    failed_gate=f"{gate_prefix}.maximum",
                    message=f"{path}: {payload} > maximum {schema['maximum']}",
                    severity=sev,
                    tool_name=tool_name,
                    field=path,
                    resolution_hint=f"{path} 不得大于 {schema['maximum']}",
                )
            )

    if isinstance(payload, str):
        if "minLength" in schema and len(payload) < schema["minLength"]:
            findings.append(
                Finding(
                    failed_gate=f"{gate_prefix}.minLength",
                    message=f"{path}: length {len(payload)} < {schema['minLength']}",
                    severity=sev,
                    tool_name=tool_name,
                    field=path,
                    resolution_hint=f"{path} 至少 {schema['minLength']} 个字符",
                )
            )
        if "maxLength" in schema and len(payload) > schema["maxLength"]:
            findings.append(
                Finding(
                    failed_gate=f"{gate_prefix}.maxLength",
                    message=f"{path}: length {len(payload)} > {schema['maxLength']}",
                    severity=sev,
                    tool_name=tool_name,
                    field=path,
                    resolution_hint=f"{path} 最多 {schema['maxLength']} 个字符",
                )
            )
        pattern = schema.get("pattern")
        if pattern and not re.search(pattern, payload):
            findings.append(
                Finding(
                    failed_gate=f"{gate_prefix}.pattern",
                    message=f"{path}: {payload!r} does not match /{pattern}/",
                    severity=sev,
                    tool_name=tool_name,
                    field=path,
                    resolution_hint=f"{path} 必须匹配正则 /{pattern}/",
                )
            )

    if isinstance(payload, (list, tuple)):
        if "minItems" in schema and len(payload) < schema["minItems"]:
            findings.append(
                Finding(
                    failed_gate=f"{gate_prefix}.minItems",
                    message=f"{path}: {len(payload)} items < {schema['minItems']}",
                    severity=sev,
                    tool_name=tool_name,
                    field=path,
                    resolution_hint=f"{path} 至少 {schema['minItems']} 项",
                )
            )
        if "maxItems" in schema and len(payload) > schema["maxItems"]:
            findings.append(
                Finding(
                    failed_gate=f"{gate_prefix}.maxItems",
                    message=f"{path}: {len(payload)} items > {schema['maxItems']}",
                    severity=sev,
                    tool_name=tool_name,
                    field=path,
                    resolution_hint=f"{path} 最多 {schema['maxItems']} 项",
                )
            )
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(payload):
                findings.extend(
                    validate_json_schema(
                        item,
                        item_schema,
                        path=f"{path}[{index}]",
                        gate_prefix=gate_prefix,
                        tool_name=tool_name,
                        severity=severity,
                    )
                )

    return findings


# ---------------------------------------------------------------------------
# Tool gate
# ---------------------------------------------------------------------------


@dataclass
class ToolSpec:
    """Declared capability of one tool.

    ``high_risk`` tools are never executed directly: the engine suspends the run
    and asks a human. The model can *see* them (they are whitelisted) but it
    cannot *authorise* them — that is the whole tool-orchestration argument.
    """

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})
    high_risk: bool = False


@dataclass
class ToolGuard:
    """Whitelist + argument schema + risk classification."""

    whitelist: Sequence[str]
    specs: dict[str, ToolSpec] = field(default_factory=dict)
    high_risk_tools: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.whitelist = list(self.whitelist)

    def is_allowed(self, tool_name: str) -> bool:
        return tool_name in self.whitelist

    def is_high_risk(self, tool_name: str) -> bool:
        if tool_name in self.high_risk_tools:
            return True
        spec = self.specs.get(tool_name)
        return bool(spec and spec.high_risk)

    def check(self, tool_name: str, arguments: dict[str, Any]) -> list[Finding]:
        """Return findings for one requested call. Empty list means "may run"."""
        if not self.is_allowed(tool_name):
            return [
                Finding(
                    failed_gate=ERROR_TOOL_NOT_ALLOWED,
                    message=f"tool {tool_name!r} is not in the whitelist",
                    tool_name=tool_name,
                    resolution_hint=(
                        f"只能调用白名单内的工具 {sorted(self.whitelist)}；"
                        f"{tool_name!r} 未被授权"
                    ),
                )
            ]

        if not isinstance(arguments, dict):
            return [
                Finding(
                    failed_gate=ERROR_TOOL_SCHEMA,
                    message=f"arguments must be an object, got {_json_type_name(arguments)}",
                    tool_name=tool_name,
                    resolution_hint="arguments 必须是 JSON 对象",
                )
            ]

        spec = self.specs.get(tool_name)
        if spec is None:
            return []
        return validate_json_schema(
            arguments,
            spec.input_schema,
            gate_prefix=ERROR_TOOL_SCHEMA,
            tool_name=tool_name,
        )


# ---------------------------------------------------------------------------
# Output gate
# ---------------------------------------------------------------------------


@dataclass
class BusinessRule:
    """A declarative rule evaluated against the model's structured output.

    ``field`` may be dotted (``order.amount``). ``op`` is one of
    ``required|gt|ge|lt|le|eq|ne|in|regex|max_length|min_length``.
    """

    name: str
    op: str
    field: str | None = None
    value: Any = None
    message: str = ""
    severity: Severity = "BLOCK"

    def evaluate(self, payload: Any) -> bool:
        target = _resolve_field(payload, self.field)
        op = self.op
        if op == "required":
            return target is not None
        if target is None:
            return False
        if op == "gt":
            return _comparable(target, self.value) and target > self.value
        if op == "ge":
            return _comparable(target, self.value) and target >= self.value
        if op == "lt":
            return _comparable(target, self.value) and target < self.value
        if op == "le":
            return _comparable(target, self.value) and target <= self.value
        if op == "eq":
            return bool(target == self.value)
        if op == "ne":
            return bool(target != self.value)
        if op == "in":
            return target in (self.value or [])
        if op == "regex":
            return isinstance(target, str) and re.search(str(self.value), target) is not None
        if op == "max_length":
            return hasattr(target, "__len__") and len(target) <= int(self.value)
        if op == "min_length":
            return hasattr(target, "__len__") and len(target) >= int(self.value)
        # An unknown operator is a configuration bug, not a payload bug: fail loudly
        # as a BLOCK so it cannot silently pass everything.
        return False

    def to_finding(self, payload: Any) -> Finding:
        target = _resolve_field(payload, self.field)
        detail = self.message or (
            f"{self.field} = {target!r} violates {self.op} {self.value!r}"
        )
        return Finding(
            failed_gate=f"{ERROR_OUTPUT_RULE}.{self.name}",
            message=detail,
            severity=self.severity,
            field=self.field,
            resolution_hint=detail,
        )


def _comparable(left: Any, right: Any) -> bool:
    """True when ``left`` and ``right`` support ordering against each other.

    ``bool`` is deliberately excluded: Python orders it as ``int``, but a rule that
    compares a flag to a number is a modelling error, not a satisfied rule.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    try:
        _ = left < right
        return True
    except TypeError:
        return False


def _resolve_field(payload: Any, path: str | None) -> Any:
    if not path:
        return payload
    current = payload
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, (list, tuple)) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
        if current is None:
            return None
    return current


RuleFn = Callable[[Any], bool]


@dataclass
class OutputGuard:
    """Deterministic checks applied to whatever the model produced."""

    output_schema: dict[str, Any] | None = None
    forbidden_strings: Sequence[str] = field(default_factory=tuple)
    forbidden_patterns: Sequence[str] = field(default_factory=tuple)
    rules: list[BusinessRule] = field(default_factory=list)
    custom_rules: list[tuple[str, RuleFn]] = field(default_factory=list)
    #: WARN findings never trigger a retry; they are reported and the run passes.
    severity: Severity = "BLOCK"

    def register_rule(self, name: str, fn: RuleFn, *, severity: Severity = "BLOCK") -> None:
        self.custom_rules.append((f"{name}|{severity}", fn))

    def check(self, payload: Any) -> list[Finding]:
        findings: list[Finding] = []

        if self.output_schema is not None:
            findings.extend(
                validate_json_schema(
                    payload,
                    self.output_schema,
                    gate_prefix=ERROR_OUTPUT_SCHEMA,
                    severity=self.severity,
                )
            )

        text = _flatten_text(payload)
        for needle in self.forbidden_strings:
            if needle and needle in text:
                findings.append(
                    Finding(
                        failed_gate=ERROR_OUTPUT_FORBIDDEN,
                        message=f"output contains forbidden string {needle!r}",
                        severity=self.severity,
                        resolution_hint=f"删除或改写包含 {needle!r} 的内容后重新输出",
                    )
                )
        for pattern in self.forbidden_patterns:
            try:
                match = re.search(pattern, text)
            except re.error as exc:  # a broken config regex must not crash the run
                findings.append(
                    Finding(
                        failed_gate=f"{ERROR_OUTPUT_FORBIDDEN}.config_error",
                        message=f"invalid regex {pattern!r}: {exc}",
                        severity="BLOCK",
                        resolution_hint="修正配置中的 forbidden_patterns 正则",
                    )
                )
                continue
            if match:
                findings.append(
                    Finding(
                        failed_gate=ERROR_OUTPUT_FORBIDDEN,
                        message=f"output matches forbidden pattern /{pattern}/ ({match.group(0)!r})",
                        severity=self.severity,
                        resolution_hint=f"输出不得匹配 /{pattern}/，请移除命中片段",
                    )
                )

        for rule in self.rules:
            try:
                ok = rule.evaluate(payload)
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                findings.append(
                    Finding(
                        failed_gate=f"{ERROR_OUTPUT_RULE}.{rule.name}.eval_error",
                        message=f"rule {rule.name} raised {type(exc).__name__}: {exc}",
                        severity="BLOCK",
                        resolution_hint=f"规则 {rule.name} 自身抛异常，先修规则",
                    )
                )
                continue
            if not ok:
                findings.append(rule.to_finding(payload))

        for tagged_name, fn in self.custom_rules:
            name, _, raw_severity = tagged_name.partition("|")
            severity = coerce_severity(raw_severity)
            try:
                ok = fn(payload)
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                findings.append(
                    Finding(
                        failed_gate=f"{ERROR_OUTPUT_RULE}.{name}.eval_error",
                        message=f"custom rule {name} raised {type(exc).__name__}: {exc}",
                        severity="BLOCK",
                        resolution_hint=f"自定义规则 {name} 抛异常，先修规则",
                    )
                )
                continue
            if not ok:
                findings.append(
                    Finding(
                        failed_gate=f"{ERROR_OUTPUT_RULE}.{name}",
                        message=f"custom rule {name} rejected the output",
                        severity=severity,
                        resolution_hint=f"输出未满足业务规则 {name}",
                    )
                )

        return findings


def _flatten_text(payload: Any) -> str:
    """Serialise any payload to text so content filters can scan it uniformly."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, (dict, list, tuple)):
        import json

        try:
            return json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError):
            return repr(payload)
    return str(payload)


def findings_from_strings(items: Iterable[str], gate: str) -> list[Finding]:
    """Small helper for adapters that only have strings to report."""
    return [Finding(failed_gate=gate, message=str(item)) for item in items]
