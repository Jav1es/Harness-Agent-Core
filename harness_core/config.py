"""Configuration loading: YAML / JSON files plus ``HARNESS_*`` env overrides.

Priority (lowest to highest): dataclass defaults -> config file -> environment.
That order is deliberate: a checked-in config file must never be able to override
an operator's environment during an incident.

YAML is optional. Without PyYAML the loader still handles JSON, and YAML files
raise a clear error telling you which extra to install.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .errors import coerce_severity
from .guards import BusinessRule, OutputGuard, ToolSpec
from .redact import OutputRedactor

ENV_PREFIX = "HARNESS_"

#: Env var -> (config attribute, coercion)
_ENV_MAP: dict[str, tuple[str, str]] = {
    "HARNESS_MAX_RETRIES": ("max_retries", "int"),
    "HARNESS_TRACE_JSONL": ("trace_jsonl", "str"),
    "HARNESS_TOOL_WHITELIST": ("tool_whitelist", "csv"),
    "HARNESS_HIGH_RISK_TOOLS": ("high_risk_tools", "csv"),
    "HARNESS_REPEAT_THRESHOLD": ("repeat_threshold", "int"),
    "HARNESS_TOKEN_BUDGET": ("token_budget", "int"),
    "HARNESS_VERBOSE": ("verbose", "bool"),
}


@dataclass
class HarnessConfig:
    """Everything the engine needs that is not code.

    Attributes
    ----------
    max_retries:
        Repair rounds before the retry cap trips.
    tool_whitelist / high_risk_tools:
        The authorisation surface. Tools absent from the whitelist are rejected
        before the connector is touched.
    tools:
        Argument schemas keyed by tool name.
    output_schema / forbidden_strings / forbidden_patterns / rules:
        Deterministic output validation.
    """

    max_retries: int = 3
    repeat_threshold: int = 1
    token_budget: int = 0
    tool_whitelist: list[str] = field(default_factory=list)
    high_risk_tools: list[str] = field(default_factory=list)
    tools: dict[str, ToolSpec] = field(default_factory=dict)
    #: Optional real MCP server: ``{"command": "...", "args": [...], "timeout": 10}``.
    #: When set, the CLI talks to it instead of the built-in fake connector.
    mcp_stdio: dict[str, Any] | None = None
    #: Optional real MCP server over HTTP/SSE: ``{"url": "...", "token_env": "..."}``.
    mcp_http: dict[str, Any] | None = None
    #: Optional real model endpoint: ``{"base_url": "...", "model": "...",
    #: "api_key_env": "LLM_API_KEY"}``. When set, the CLI calls it instead of the
    #: scripted planner. The key is read from the environment, never from config.
    llm: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    forbidden_strings: list[str] = field(default_factory=list)
    forbidden_patterns: list[str] = field(default_factory=list)
    rules: list[BusinessRule] = field(default_factory=list)
    #: Output-side redaction. ``False`` (the default) leaves deliverables untouched:
    #: silently rewriting a caller's payload would be a surprising default.
    redact: bool = False
    #: Restrict redaction to these leaf field names. Empty means "walk everything".
    redact_fields: list[str] = field(default_factory=list)
    trace_jsonl: str | None = None
    verbose: bool = False

    # ------------------------------------------------------------------ API
    def output_guard(self) -> OutputGuard:
        return OutputGuard(
            output_schema=self.output_schema,
            forbidden_strings=list(self.forbidden_strings),
            forbidden_patterns=list(self.forbidden_patterns),
            rules=list(self.rules),
        )

    def redactor(self) -> OutputRedactor | None:
        """Build the redactor, or ``None`` when redaction is off."""
        if not self.redact:
            return None
        return OutputRedactor(fields=list(self.redact_fields) or None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_retries": self.max_retries,
            "repeat_threshold": self.repeat_threshold,
            "token_budget": self.token_budget,
            "tool_whitelist": list(self.tool_whitelist),
            "high_risk_tools": list(self.high_risk_tools),
            "tools": {
                name: {
                    "description": spec.description,
                    "input_schema": spec.input_schema,
                    "high_risk": spec.high_risk,
                }
                for name, spec in self.tools.items()
            },
            "mcp_stdio": dict(self.mcp_stdio) if self.mcp_stdio else None,
            "mcp_http": dict(self.mcp_http) if self.mcp_http else None,
            "llm": dict(self.llm) if self.llm else None,
            "output_schema": self.output_schema,
            "forbidden_strings": list(self.forbidden_strings),
            "forbidden_patterns": list(self.forbidden_patterns),
            "redact": self.redact,
            "redact_fields": list(self.redact_fields),
            "rules": [
                {
                    "name": rule.name,
                    "op": rule.op,
                    "field": rule.field,
                    "value": rule.value,
                    "message": rule.message,
                    "severity": rule.severity,
                }
                for rule in self.rules
            ],
            "trace_jsonl": self.trace_jsonl,
            "verbose": self.verbose,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "HarnessConfig":
        tools: dict[str, ToolSpec] = {}
        raw_tools = data.get("tools") or {}
        if not isinstance(raw_tools, dict):
            raise ConfigError("'tools' must be a mapping of tool name -> schema")
        for name, raw in raw_tools.items():
            if not isinstance(raw, dict):
                raise ConfigError(f"tool {name!r} must be a mapping")
            tools[name] = ToolSpec(
                name=name,
                description=str(raw.get("description", "")),
                input_schema=raw.get("input_schema") or raw.get("inputSchema") or {"type": "object"},
                high_risk=bool(raw.get("high_risk", False)),
            )

        rules: list[BusinessRule] = []
        for raw_rule in data.get("rules") or []:
            if not isinstance(raw_rule, dict) or "name" not in raw_rule or "op" not in raw_rule:
                raise ConfigError("each business rule needs at least 'name' and 'op'")
            rules.append(
                BusinessRule(
                    name=str(raw_rule["name"]),
                    op=str(raw_rule["op"]),
                    field=raw_rule.get("field"),
                    value=raw_rule.get("value"),
                    message=str(raw_rule.get("message", "")),
                    # An unrecognised severity must not silently behave like "not
                    # BLOCK" and let a failure through.
                    severity=coerce_severity(raw_rule.get("severity")),
                )
            )

        high_risk = list(data.get("high_risk_tools") or [])
        for spec in tools.values():
            if spec.high_risk and spec.name not in high_risk:
                high_risk.append(spec.name)

        mcp_stdio = data.get("mcp_stdio")
        if mcp_stdio is not None:
            if not isinstance(mcp_stdio, dict) or not mcp_stdio.get("command"):
                raise ConfigError("'mcp_stdio' must be a mapping with a 'command'")
            mcp_stdio = dict(mcp_stdio)

        mcp_http = data.get("mcp_http")
        if mcp_http is not None:
            if not isinstance(mcp_http, dict) or not mcp_http.get("url"):
                raise ConfigError("'mcp_http' must be a mapping with a 'url'")
            if "headers" in mcp_http and not isinstance(mcp_http["headers"], dict):
                raise ConfigError("'mcp_http.headers' must be a mapping")
            mcp_http = dict(mcp_http)

        if mcp_stdio and mcp_http:
            # Two transports cannot both be "the" connector. Failing loudly beats
            # picking one and leaving the other silently ignored.
            raise ConfigError(
                "configure only one of 'mcp_stdio' / 'mcp_http'; "
                "both were set, so the run would use whichever wins arbitrarily"
            )

        llm = data.get("llm")
        if llm is not None:
            if not isinstance(llm, dict) or not llm.get("base_url") or not llm.get("model"):
                raise ConfigError("'llm' must be a mapping with 'base_url' and 'model'")
            llm = dict(llm)
            # A key in the config file is a leaked secret. Require the env var name.
            if "api_key" in llm:
                raise ConfigError(
                    "'llm.api_key' is not allowed: put the key in an environment "
                    "variable and reference it with 'api_key_env'"
                )

        return cls(
            max_retries=int(data.get("max_retries", 3)),
            repeat_threshold=int(data.get("repeat_threshold", 1)),
            token_budget=int(data.get("token_budget", 0)),
            tool_whitelist=list(data.get("tool_whitelist") or []),
            high_risk_tools=high_risk,
            tools=tools,
            mcp_stdio=mcp_stdio,
            mcp_http=mcp_http,
            llm=llm,
            output_schema=data.get("output_schema"),
            forbidden_strings=list(data.get("forbidden_strings") or []),
            forbidden_patterns=list(data.get("forbidden_patterns") or []),
            rules=rules,
            redact=bool(data.get("redact", False)),
            redact_fields=list(data.get("redact_fields") or []),
            trace_jsonl=data.get("trace_jsonl"),
            verbose=bool(data.get("verbose", False)),
        )

    @classmethod
    def load(cls, path: str | Path | None = None, *, env: Mapping[str, str] | None = None) -> "HarnessConfig":
        """Load from a file (YAML/JSON) then apply env overrides."""
        config = cls()
        if path is not None:
            config = cls._load_file(Path(path))
        return config.with_env(env)

    @classmethod
    def _load_file(cls, path: Path) -> "HarnessConfig":
        if not path.exists():
            raise ConfigError(f"config file not found: {path}")
        text = path.read_text(encoding="utf-8")
        suffix = path.suffix.lower()
        data: Any
        if suffix in {".yaml", ".yml"}:
            data = _load_yaml(text, path)
        elif suffix == ".json":
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ConfigError(f"invalid JSON in {path}: {exc}") from exc
        else:
            # Unknown suffix: try JSON first, then YAML.
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = _load_yaml(text, path)
        if not isinstance(data, dict):
            raise ConfigError(f"{path}: top-level config must be a mapping")
        return cls.from_dict(data)

    def with_env(self, env: Mapping[str, str] | None = None) -> "HarnessConfig":
        """Return a copy with ``HARNESS_*`` variables applied."""
        source = os.environ if env is None else env
        updated = HarnessConfig.from_dict(self.to_dict())
        for env_name, (attribute, kind) in _ENV_MAP.items():
            if env_name not in source:
                continue
            raw = source[env_name]
            if kind == "int":
                setattr(updated, attribute, int(raw))
            elif kind == "bool":
                setattr(updated, attribute, raw.strip().lower() in {"1", "true", "yes", "on"})
            elif kind == "csv":
                setattr(updated, attribute, [item.strip() for item in raw.split(",") if item.strip()])
            else:
                setattr(updated, attribute, raw)
        # Env-provided high-risk names should also flip the per-tool flag.
        for name in updated.high_risk_tools:
            spec = updated.tools.get(name)
            if spec is not None:
                spec.high_risk = True
        return updated


class ConfigError(ValueError):
    """Raised when a configuration file is malformed."""


def _load_yaml(text: str, path: Path) -> Any:
    try:
        import yaml
    except ImportError as exc:
        raise ConfigError(
            f"{path}: YAML config needs PyYAML. Install it with "
            '`pip install "harness-agent-core[yaml]"` or use JSON instead.'
        ) from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
