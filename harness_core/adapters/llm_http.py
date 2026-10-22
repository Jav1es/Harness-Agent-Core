"""A real LLM adapter for OpenAI-compatible chat-completions endpoints.

Why not the ``openai`` SDK
--------------------------
The endpoint contract is small and stable: ``POST /v1/chat/completions`` with a JSON
body, a bearer token, and a response containing ``choices[0].message`` plus
``usage``. Talking to it with ``urllib`` buys three things the SDK cannot:

* **Zero runtime dependencies.** The package must install and run anywhere.
* **No version coupling.** SDK releases move; the wire format does not.
* **Testability.** A test can run a real HTTP server in a subprocess and exercise
  retries, 429s, malformed bodies and timeouts offline — no network, no API key.

Error classification
--------------------
The distinction is the same one the MCP connector makes, and for the same reason:

* **Transport / auth / rate-limit / timeout** → :class:`HarnessInfrastructureError`.
  The model never got a chance to answer, so asking it to "repair" is nonsense.
* **A response that arrived but is unusable** (no ``choices``, empty content,
  non-JSON when JSON was requested) → a normal ``LLMResponse`` with ``error`` set,
  which the engine turns into a BLOCK finding and retries. A model that emits bad
  JSON *can* be repaired by showing it the error.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..errors import HarnessInfrastructureError
from ..state import AgentHarnessState
from .llm import LLMResponse, ToolCallRequest

#: Conservative default: long enough for a real model, short enough to fail fast in CI.
DEFAULT_TIMEOUT = 60.0


class LLMTransportError(HarnessInfrastructureError):
    """The endpoint could not be reached, refused us, or timed out."""


@dataclass
class ChatMessage:
    role: str
    content: str

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "content": self.content}


@dataclass
class HttpLLMAdapter:
    """Talks to any OpenAI-compatible ``/chat/completions`` endpoint.

    Usage::

        adapter = HttpLLMAdapter(
            base_url="https://api.example.com/v1",
            api_key=os.environ["LLM_API_KEY"],
            model="gpt-4o-mini",
        )
        engine = HarnessOrchestrationEngine(llm=adapter, connector=connector)

    ``base_url`` may be the API root (``.../v1``) or the full endpoint; a trailing
    ``/chat/completions`` is appended when missing.
    """

    base_url: str
    model: str
    api_key: str | None = None
    timeout: float = DEFAULT_TIMEOUT
    temperature: float = 0.0
    max_tokens: int | None = None
    #: Extra body fields (e.g. ``{"top_p": 0.9}``), merged last.
    extra_body: dict[str, Any] = field(default_factory=dict)
    #: Extra request headers, merged last.
    extra_headers: dict[str, str] = field(default_factory=dict)
    #: Sends ``response_format={"type": "json_object"}`` when True.
    force_json: bool = False

    name = "http"

    # ------------------------------------------------------------------ URL
    def endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    # ---------------------------------------------------------------- request
    def build_body(
        self,
        prompt: str,
        state: AgentHarnessState,
        tools: Sequence[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """Build the request body. Separated so tests can assert on it directly."""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [ChatMessage(role="user", content=prompt).to_dict()],
            "temperature": self.temperature,
        }
        if self.max_tokens is not None:
            body["max_tokens"] = self.max_tokens
        if self.force_json:
            body["response_format"] = {"type": "json_object"}
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": str(tool.get("name", "")),
                        "description": str(tool.get("description", "")),
                        "parameters": tool.get("input_schema")
                        or {"type": "object", "properties": {}},
                    },
                }
                for tool in tools
            ]
        body.update(self.extra_body)
        return body

    def headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "harness-agent-core/0.1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.extra_headers)
        return headers

    # ------------------------------------------------------------------- call
    def generate(
        self,
        prompt: str,
        state: AgentHarnessState,
        *,
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        body = self.build_body(prompt, state, tools)
        payload = self._post(self.endpoint(), body)
        return self._parse(payload, raw_request=prompt)

    def _post(self, url: str, body: Mapping[str, Any]) -> dict[str, Any]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, headers=self.headers(), method="POST"
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = _safe_read(exc)
            raise LLMTransportError(
                f"LLM endpoint returned HTTP {exc.code} for {url}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise LLMTransportError(
                f"could not reach LLM endpoint {url}: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise LLMTransportError(
                f"LLM endpoint {url} timed out after {self.timeout}s"
            ) from exc
        except OSError as exc:
            raise LLMTransportError(f"LLM transport error for {url}: {exc}") from exc
        finally:
            _ = time.perf_counter() - started

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMTransportError(
                f"LLM endpoint returned non-JSON ({len(raw)} bytes): {raw[:200]!r}"
            ) from exc
        if not isinstance(parsed, dict):
            raise LLMTransportError(f"LLM endpoint returned {type(parsed).__name__}, not an object")
        return parsed

    # ------------------------------------------------------------------ parse
    def _parse(self, payload: dict[str, Any], *, raw_request: str) -> LLMResponse:
        """Turn a chat-completions payload into an LLMResponse.

        A malformed *body* is a business failure (``error`` set), not an exception:
        the model answered, it just answered badly, and the repair loop can retry.
        """
        usage = payload.get("usage") or {}
        tokens = int(usage.get("total_tokens") or 0) or _estimate_from(payload)
        model = str(payload.get("model") or self.model)

        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return LLMResponse(
                content=None,
                raw_text=json.dumps(payload)[:2000],
                tokens_estimate=tokens,
                model=model,
                error="LLM response contained no choices",
            )
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") or {}
        if not isinstance(message, dict):
            message = {}

        tool_call = _extract_tool_call(message)
        content = message.get("content")
        text = "" if content is None else str(content)

        if tool_call is not None:
            return LLMResponse(
                content=None,
                tool_call=tool_call,
                raw_text=text,
                tokens_estimate=tokens,
                model=model,
            )

        if not text.strip():
            return LLMResponse(
                content=None,
                raw_text="",
                tokens_estimate=tokens,
                model=model,
                error="LLM returned neither content nor a tool call",
            )

        return LLMResponse(
            content=_maybe_json(text),
            raw_text=text,
            tokens_estimate=tokens,
            model=model,
        )


def _extract_tool_call(message: dict[str, Any]) -> ToolCallRequest | None:
    """Read an OpenAI-style ``tool_calls`` array (first call wins)."""
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        return None
    first = calls[0] if isinstance(calls[0], dict) else {}
    function = first.get("function") or {}
    if not isinstance(function, dict):
        return None
    name = str(function.get("name") or "")
    if not name:
        return None
    raw_args = function.get("arguments")
    arguments: dict[str, Any] = {}
    if isinstance(raw_args, str) and raw_args.strip():
        try:
            parsed = json.loads(raw_args)
        except json.JSONDecodeError:
            # Keep the raw text so the failure is visible rather than silently empty.
            arguments = {"__raw_arguments__": raw_args}
        else:
            arguments = parsed if isinstance(parsed, dict) else {"__value__": parsed}
    elif isinstance(raw_args, dict):
        arguments = dict(raw_args)
    return ToolCallRequest(tool_name=name, arguments=arguments)


def _maybe_json(text: str) -> Any:
    """Parse JSON content when it looks like JSON; otherwise return the text.

    A deliverable is usually JSON, but not always. Returning the raw string keeps
    the validator's "this is not an object" message honest instead of pretending.
    """
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return text
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return text


def _estimate_from(payload: dict[str, Any]) -> int:
    """Fall back to a crude estimate when the endpoint omits ``usage``."""
    total = 0
    for choice in payload.get("choices") or []:
        message = (choice or {}).get("message") or {}
        total += len(str(message.get("content") or ""))
    return total // 4


def _safe_read(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except (OSError, ValueError):  # pragma: no cover - stream already consumed
        return "<no body>"
    return body[:500]
