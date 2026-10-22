"""LLM adapters.

The harness never imports a vendor SDK. It talks to :class:`LLMAdapter`, so a real
model is a plugin (``pip install harness-agent-core[openai]`` in your own project,
or simply pass your own object) and CI runs against :class:`FakeLLMAdapter` with
no network at all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

from ..errors import ERROR_LLM, Finding
from ..state import AgentHarnessState


@dataclass
class ToolCallRequest:
    """The model's *request* to call a tool. A request is not an authorisation."""

    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"tool_name": self.tool_name, "arguments": self.arguments}


@dataclass
class LLMResponse:
    """One model turn.

    ``content`` carries the structured deliverable; ``tool_call`` (optional)
    carries a requested tool invocation. A response may legitimately have both,
    but the harness will execute the tool call before accepting the content.
    """

    content: Any = None
    tool_call: ToolCallRequest | None = None
    raw_text: str = ""
    tokens_estimate: int = 0
    model: str = "fake"
    #: Set by the adapter when it could not produce a response at all.
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "tool_call": self.tool_call.to_dict() if self.tool_call else None,
            "raw_text": self.raw_text,
            "tokens_estimate": self.tokens_estimate,
            "model": self.model,
            "error": self.error,
        }


@runtime_checkable
class LLMAdapter(Protocol):
    """Minimal contract every model integration must satisfy.

    ``generate`` receives the rendered prompt (which already contains the repair
    block for retries) plus the state snapshot, and returns one :class:`LLMResponse`.
    """

    name: str

    def generate(
        self,
        prompt: str,
        state: AgentHarnessState,
        *,
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> LLMResponse: ...


class FakeLLMAdapter:
    """Scripted adapter: returns canned responses in order.

    Two scripting modes, both fully offline:

    * ``responses=[LLMResponse(...), ...]`` — a fixed script. When the script runs
      out, the last response repeats (so a "always broken" script keeps failing,
      which is exactly what the breaker tests need).
    * ``responder=callable`` — a function ``(prompt, state, attempt) -> LLMResponse``
      for scenarios that must react to the repair prompt (e.g. "fail until the
      prompt mentions ``currency``, then succeed").

    ``prompts`` records every prompt the engine sent, which the tests use to
    assert that the structured error block was actually fed back.
    """

    name = "fake"

    def __init__(
        self,
        responses: Sequence[LLMResponse] | None = None,
        *,
        responder: Callable[[str, AgentHarnessState, int], LLMResponse] | None = None,
        repeat_last: bool = True,
    ) -> None:
        self._responses: list[LLMResponse] = list(responses or [])
        self._responder = responder
        self._repeat_last = repeat_last
        self.prompts: list[str] = []
        self.calls = 0

    # ------------------------------------------------------------------ API
    def generate(
        self,
        prompt: str,
        state: AgentHarnessState,
        *,
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        self.prompts.append(prompt)
        attempt = self.calls
        self.calls += 1

        if self._responder is not None:
            return self._responder(prompt, state, attempt)

        if not self._responses:
            return LLMResponse(
                content=None,
                raw_text="",
                error="FakeLLMAdapter has no scripted responses",
                tokens_estimate=0,
            )

        index = min(attempt, len(self._responses) - 1)
        if not self._repeat_last and attempt >= len(self._responses):
            return LLMResponse(
                content=None,
                error=f"script exhausted after {len(self._responses)} responses",
            )
        return self._responses[index]

    # -------------------------------------------------------------- helpers
    @property
    def prompt_count(self) -> int:
        return len(self.prompts)

    def last_prompt(self) -> str:
        return self.prompts[-1] if self.prompts else ""


def json_response(
    payload: dict[str, Any] | None = None,
    *,
    tool_name: str | None = None,
    arguments: dict[str, Any] | None = None,
    model: str = "fake",
) -> LLMResponse:
    """Convenience constructor for scripted responses."""
    text = json.dumps(payload, ensure_ascii=False) if payload is not None else ""
    return LLMResponse(
        content=payload,
        tool_call=(
            ToolCallRequest(tool_name=tool_name, arguments=arguments or {})
            if tool_name
            else None
        ),
        raw_text=text,
        tokens_estimate=max(1, len(text) // 4) if text else 0,
        model=model,
    )


class LLMError(Exception):
    """Raised by real adapters when the provider fails."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


def llm_finding(message: str) -> Finding:
    """Wrap an adapter failure as a BLOCK finding so it joins the normal loop."""
    return Finding(
        failed_gate=ERROR_LLM,
        message=message,
        resolution_hint="模型调用失败；检查适配器配置或稍后重试",
    )


class CallableLLMAdapter(FakeLLMAdapter):
    """Alias kept for readability in examples: a fake driven by a function."""

    def __init__(self, responder: Callable[[str, AgentHarnessState, int], LLMResponse]) -> None:
        super().__init__(responder=responder)
        self.name = "callable"
