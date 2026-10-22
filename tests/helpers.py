"""Shared test data and builders (no fixtures — those live in conftest)."""

from __future__ import annotations

import json
from typing import Any

from harness_core.adapters.llm import LLMResponse, ToolCallRequest

ORDER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["order_id", "currency", "amount", "status"],
    "additionalProperties": False,
    "properties": {
        "order_id": {"type": "string", "minLength": 3},
        "currency": {"type": "string", "enum": ["CNY", "USD"]},
        "amount": {"type": "number", "minimum": 0, "maximum": 100000},
        "status": {"type": "string", "enum": ["settled", "rejected"]},
    },
}

FETCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["order_id"],
    "additionalProperties": False,
    "properties": {"order_id": {"type": "string", "minLength": 3}},
}

WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["order_id", "amount"],
    "additionalProperties": False,
    "properties": {
        "order_id": {"type": "string", "minLength": 3},
        "amount": {"type": "number", "minimum": 0},
    },
}

DELETE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["id"],
    "properties": {"id": {"type": "string"}},
}

VALID: dict[str, Any] = {
    "order_id": "ORD-1001",
    "currency": "CNY",
    "amount": 1200.0,
    "status": "settled",
}


def response_for(payload: dict[str, Any] | None = None, **kwargs: Any) -> LLMResponse:
    body = payload if payload is not None else VALID
    return LLMResponse(
        content=body,
        raw_text=json.dumps(body),
        tokens_estimate=40,
        **kwargs,
    )


def tool_response(tool_name: str, arguments: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        tool_call=ToolCallRequest(tool_name, arguments),
        raw_text=json.dumps({"tool_name": tool_name, "arguments": arguments}),
        tokens_estimate=24,
    )
