"""Harness-Agent-Core: a deterministic harness layer between an LLM and business systems.

Quick start::

    from harness_core import HarnessOrchestrationEngine, AgentHarnessState
    from harness_core.adapters import FakeLLMAdapter, FakeMCPConnector

The package has **zero runtime dependencies** and runs fully offline, which is what
makes it usable as a CI gate.
"""

from __future__ import annotations

from .circuit_breaker import (
    BREAKER_BUDGET,
    BREAKER_CONSECUTIVE,
    BREAKER_MAX_RETRIES,
    BREAKER_RECURRED,
    BreakerDecision,
    CircuitBreaker,
)
from .config import ConfigError, HarnessConfig
from .engine import HarnessOrchestrationEngine, HarnessRunResult, ValidatorFn
from .errors import ErrorStack, Finding
from .guards import (
    BusinessRule,
    OutputGuard,
    ToolGuard,
    ToolSpec,
    validate_json_schema,
)
from .hitl import (
    ApprovalRequest,
    ApprovalResponse,
    AutoApprover,
    FakeWebhookNotifier,
    HumanApprovalGateway,
    WebhookNotifier,
)
from .observability import JsonlSink, MemorySink, StructuredLogger, Tracer, TraceSpan
from .state import AgentHarnessState, HistoryEntry, RunStatus, ToolCall
from .loop import (
    DRIVERS,
    Action,
    GraphLoopDriver,
    LoopContext,
    LoopEngine,
    LoopOutcome,
    WhileLoopDriver,
    get_driver,
)
from .replay import (
    CASSETTE_VERSION,
    Cassette,
    CassetteEntry,
    CassetteMismatch,
    CassetteVersionError,
    Recorder,
    ReplayError,
    ReplayExhausted,
    migrate,
)
from .semantic import LLMJudgeGate, SemanticLayer, SemanticVerdict, build_judge
from .untrusted import scan, scan_payload, wrap_untrusted
from .durable import (
    SNAPSHOT_VERSION,
    ResumeError,
    RunSnapshot,
    restore_into,
    snapshot_of,
)
from .redact import (
    DEFAULT_REDACTORS,
    ERROR_REDACTED,
    OutputRedactor,
    Redactor,
    redact,
)

__version__ = "0.1.0"

__all__ = [
    # state
    "AgentHarnessState",
    "HistoryEntry",
    "RunStatus",
    "ToolCall",
    # errors
    "ErrorStack",
    "Finding",
    # engine
    "HarnessOrchestrationEngine",
    "HarnessRunResult",
    "ValidatorFn",
    # guards
    "BusinessRule",
    "OutputGuard",
    "ToolGuard",
    "ToolSpec",
    "validate_json_schema",
    # circuit breaker
    "CircuitBreaker",
    "BreakerDecision",
    "BREAKER_MAX_RETRIES",
    "BREAKER_RECURRED",
    "BREAKER_CONSECUTIVE",
    "BREAKER_BUDGET",
    # hitl
    "ApprovalRequest",
    "ApprovalResponse",
    "AutoApprover",
    "FakeWebhookNotifier",
    "HumanApprovalGateway",
    "WebhookNotifier",
    # loop drivers
    "Action",
    "DRIVERS",
    "GraphLoopDriver",
    "LoopContext",
    "LoopEngine",
    "LoopOutcome",
    "WhileLoopDriver",
    "get_driver",
    # observability
    "JsonlSink",
    "MemorySink",
    "StructuredLogger",
    "Tracer",
    "TraceSpan",
    # config
    "HarnessConfig",
    "ConfigError",
    # replay
    "CASSETTE_VERSION",
    "Cassette",
    "CassetteEntry",
    "CassetteMismatch",
    "CassetteVersionError",
    "Recorder",
    "ReplayError",
    "ReplayExhausted",
    "migrate",
    # semantic layer
    "LLMJudgeGate",
    "SemanticLayer",
    "SemanticVerdict",
    "build_judge",
    # untrusted content
    "scan",
    "scan_payload",
    "wrap_untrusted",
    # durable resume
    "SNAPSHOT_VERSION",
    "ResumeError",
    "RunSnapshot",
    "restore_into",
    "snapshot_of",
    # output redaction
    "DEFAULT_REDACTORS",
    "ERROR_REDACTED",
    "OutputRedactor",
    "Redactor",
    "redact",
    "__version__",
]
