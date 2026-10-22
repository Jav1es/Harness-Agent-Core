"""Adapters: model providers and tool transports live behind these protocols."""

from .llm import (
    CallableLLMAdapter,
    FakeLLMAdapter,
    LLMAdapter,
    LLMError,
    LLMResponse,
    ToolCallRequest,
    json_response,
)
from .mcp import (
    FakeMCPConnector,
    MCPConnector,
    StdioMCPConnector,
    ToolDefinition,
    ToolResult,
)

__all__ = [
    "CallableLLMAdapter",
    "FakeLLMAdapter",
    "FakeMCPConnector",
    "LLMAdapter",
    "LLMError",
    "LLMResponse",
    "MCPConnector",
    "StdioMCPConnector",
    "ToolCallRequest",
    "ToolDefinition",
    "ToolResult",
    "json_response",
]
