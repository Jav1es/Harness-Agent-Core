#!/usr/bin/env python
"""Runnable demo: the full self-healing chain, printed turn by turn.

Run it with::

    python examples/self_healing_demo.py

It uses only the offline fakes, so it works with no API key and no network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow running the file directly from a checkout without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_core.adapters.llm import FakeLLMAdapter, LLMResponse, ToolCallRequest
from harness_core.scenarios import (  # noqa: E402
    ORDER_OUTPUT_SCHEMA,
    VALID_RESULT,
    build_connector,
    build_engine,
)


def main() -> int:
    responses = [
        # Turn 1 — ask for a tool that was never whitelisted.
        LLMResponse(
            tool_call=ToolCallRequest("drop_database", {"db": "orders"}),
            raw_text="<request drop_database>",
            tokens_estimate=18,
        ),
        # Turn 2 — whitelisted tool, wrong argument name.
        LLMResponse(
            tool_call=ToolCallRequest("fetch_order", {"order": "ORD-1001"}),
            raw_text="<request fetch_order(order=...)>",
            tokens_estimate=22,
        ),
        # Turn 3 — correct call; the connector executes it.
        LLMResponse(
            tool_call=ToolCallRequest("fetch_order", {"order_id": "ORD-1001"}),
            raw_text="<request fetch_order(order_id=ORD-1001)>",
            tokens_estimate=24,
        ),
        # Turn 4 — deliverable breaks the business rule (amount must be > 0).
        LLMResponse(
            content={**VALID_RESULT, "amount": 0.0},
            raw_text=json.dumps({**VALID_RESULT, "amount": 0.0}),
            tokens_estimate=38,
        ),
        # Turn 5 — valid deliverable.
        LLMResponse(content=VALID_RESULT, raw_text=json.dumps(VALID_RESULT), tokens_estimate=40),
    ]

    connector = build_connector()
    llm = FakeLLMAdapter(responses)
    engine = build_engine(llm, connector=connector, max_retries=4)

    print("=" * 78)
    print("Harness-Agent-Core · self-healing walkthrough")
    print("=" * 78)
    print("task: Settle order ORD-1001 for ACME")
    print("whitelist: fetch_order, compute_total, write_api")
    print(f"output schema: {sorted(ORDER_OUTPUT_SCHEMA['required'])}")
    print()

    result = engine.run(
        "Settle order ORD-1001 for ACME",
        tool_whitelist=["fetch_order", "compute_total", "write_api"],
    )

    for index, prompt in enumerate(llm.prompts, start=1):
        print(f"--- model turn {index} ---")
        repair = [line for line in prompt.splitlines() if line.startswith("- [")]
        if repair:
            print("  repair block fed back to the model:")
            for line in repair:
                print(f"    {line}")
        elif index == 1:
            print("  (first turn: no repair block)")
        else:
            print("  (no repair block: the previous turn produced no blocking finding)")
        print()

    print("=" * 78)
    print(f"status        : {result.status.value}")
    print(f"trace_id      : {result.trace_id}")
    print(f"model turns   : {result.attempts}")
    print(f"retries       : {result.state.retry_count if result.state else 0}")
    print(f"tokens (est.) : {result.tokens_estimate}")
    print(f"duration      : {result.duration_ms:.2f} ms")
    print(f"deliverable   : {json.dumps(result.execution_result, ensure_ascii=False)}")
    print(f"connector saw : {connector.calls}")
    print("=" * 78)

    ok = (
        result.status.value == "SELF_HEALED"
        and connector.call_count("fetch_order") == 1
        and "drop_database" not in connector.called_tools()
    )
    print("RESULT:", "OK" if ok else "UNEXPECTED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
