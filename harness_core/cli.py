"""Command line interface: ``harness-core run | demo | eval``.

Exit codes are the CI contract:

* ``0`` — the command did what it promised.
* ``1`` — a run failed / a scenario failed / an unexpected internal error.
* ``2`` — usage or configuration error (argparse's own convention).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from . import __version__
from .adapters.llm import FakeLLMAdapter, LLMResponse
from .config import ConfigError, HarnessConfig
from .engine import HarnessOrchestrationEngine
from .hitl import AutoApprover, FakeWebhookNotifier, HumanApprovalGateway
from .loop import DRIVERS
from .replay import (
    Cassette,
    Recorder,
    RecordingLLMAdapter,
    RecordingMCPConnector,
    ReplayError,
    ReplayLLMAdapter,
    ReplayMCPConnector,
    summarize,
)
from .observability import JsonlSink, StructuredLogger, Tracer
from .guards import OutputGuard, ToolSpec
from .scenarios import (
    SCENARIOS,
    build_connector,
    build_output_guard,
    build_tool_specs,
    demo_scenarios,
    run_all,
    run_scenario,
)
from .state import AgentHarnessState, RunStatus

STATUS_COLORS = {
    "FIRST_PASS": "\033[32m",
    "SELF_HEALED": "\033[32m",
    "OK_WITH_WARNINGS": "\033[33m",
    "SUCCEEDED": "\033[32m",
    "PENDING_HUMAN_APPROVAL": "\033[36m",
    "FAILED_WITH_CIRCUIT_BREAKER": "\033[31m",
    "FAILED": "\033[31m",
    "REJECTED_BY_HUMAN": "\033[31m",
}
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"


def _configure_stdio() -> None:
    """Force UTF-8 on stdout/stderr.

    Windows consoles default to a legacy code page (GBK on this machine), which
    turns any box-drawing or arrow glyph into ``UnicodeEncodeError``. Reconfiguring
    is enough; ``errors="replace"`` keeps a redirected pipe from crashing.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            continue


#: Set by ``--no-color`` / ``NO_COLOR``; read by :func:`paint`.
_COLOR_ENABLED = True


def _supports_color() -> bool:
    if not _COLOR_ENABLED:
        return False
    import os

    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    return sys.stdout.isatty()


def _env_flag(name: str) -> bool:
    import os

    return bool(os.environ.get(name))


def paint(text: str, color: str) -> str:
    if not _supports_color():
        return text
    return f"{color}{text}{RESET}"


def status_badge(status: RunStatus) -> str:
    color = STATUS_COLORS.get(status.value, "")
    return paint(status.value, color) if color else status.value


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _guard_from_config(config: HarnessConfig) -> OutputGuard:
    """Build the output contract from config, falling back to the built-in default.

    Shared by ``run`` and ``replay`` on purpose: if the two drifted, a replay could
    pass under a laxer contract than the run it is supposed to reproduce.
    """
    guard = build_output_guard()
    if config.output_schema is not None:
        guard.output_schema = config.output_schema
    if config.forbidden_strings:
        guard.forbidden_strings = list(config.forbidden_strings)
    if config.forbidden_patterns:
        guard.forbidden_patterns = list(config.forbidden_patterns)
    if config.rules:
        guard.rules = list(config.rules)
    return guard


def _specs_from_config(config: HarnessConfig) -> dict[str, ToolSpec]:
    """Tool specs, with any config-declared tool overriding the built-in default."""
    specs = build_tool_specs()
    for name, spec in config.tools.items():
        specs[name] = spec
    return specs


def _connector_from_config(config: HarnessConfig) -> Any:
    """Use a real MCP server when one is configured, else the offline fake.

    Returning the fake by default is what keeps ``harness-core run`` working on a
    fresh machine with no server installed. The switch is explicit in config, never
    implicit, so a run can never silently talk to the wrong transport.
    """
    if config.mcp_stdio:
        from .adapters.mcp_stdio import StdioMCPConnector

        settings = dict(config.mcp_stdio)
        command = str(settings.pop("command"))
        args = settings.pop("args", None) or []
        timeout = float(settings.pop("timeout", 10.0))
        env = settings.pop("env", None)
        cwd = settings.pop("cwd", None)
        if settings:
            raise ConfigError(
                f"unknown mcp_stdio keys: {sorted(settings)}; "
                "allowed: command, args, env, cwd, timeout"
            )
        return StdioMCPConnector(
            command,
            list(args),
            env=dict(env) if env else None,
            cwd=cwd,
            timeout=timeout,
        )
    if config.mcp_http:
        from .adapters.mcp_http import HttpMCPConnector

        settings = dict(config.mcp_http)
        url = str(settings.pop("url"))
        headers = settings.pop("headers", None) or {}
        timeout = float(settings.pop("timeout", 30.0))
        token_env = settings.pop("token_env", None)
        if settings:
            raise ConfigError(
                f"unknown mcp_http keys: {sorted(settings)}; "
                "allowed: url, headers, timeout, token_env"
            )
        # A token in the config file would end up in git; require an env var name.
        if token_env:
            token = os.environ.get(str(token_env))
            if not token:
                raise ConfigError(
                    f"mcp_http.token_env points at {token_env!r} but that environment "
                    "variable is empty; set it before running"
                )
            headers = {**headers, "Authorization": f"Bearer {token}"}
        return HttpMCPConnector(url, headers=dict(headers), timeout=timeout)
    return build_connector()


def _llm_from_config(config: HarnessConfig, whitelist: Sequence[str]) -> Any:
    """Use a real model endpoint when one is configured, else the scripted planner.

    The API key is read from the environment via ``api_key_env``; a key written into
    the config file is rejected at parse time, because a config file is the kind of
    thing that ends up in git.
    """
    if not config.llm:
        return FakeLLMAdapter(responder=_cli_responder(whitelist))

    from .adapters.llm_http import HttpLLMAdapter

    settings = dict(config.llm)
    base_url = str(settings.pop("base_url"))
    model = str(settings.pop("model"))
    key_env = settings.pop("api_key_env", None)
    timeout = float(settings.pop("timeout", 60.0))
    temperature = float(settings.pop("temperature", 0.0))
    force_json = bool(settings.pop("force_json", False))
    extra_body = settings.pop("extra_body", None)
    if settings:
        raise ConfigError(
            f"unknown llm keys: {sorted(settings)}; allowed: base_url, model, "
            "api_key_env, timeout, temperature, force_json, extra_body"
        )
    api_key = os.environ.get(str(key_env)) if key_env else None
    if key_env and not api_key:
        raise ConfigError(
            f"llm.api_key_env points at {key_env!r} but that environment variable is "
            "empty; set it before running"
        )
    return HttpLLMAdapter(
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout=timeout,
        temperature=temperature,
        force_json=force_json,
        extra_body=dict(extra_body) if extra_body else {},
    )


def _build_from_config(
    config: HarnessConfig,
    *,
    recorder: Recorder | None = None,
) -> HarnessOrchestrationEngine:
    """Wire an engine from config.

    Without a real model/transport configured, the CLI uses the offline fakes so
    ``harness-core run`` always works on a fresh machine. Real adapters are injected
    by importing the package as a library (see README).

    ``recorder`` wraps both adapters so the run can be replayed later; the wrapping
    happens here, not in the engine, so the engine stays unaware of recording.
    """
    connector: Any = _connector_from_config(config)
    specs = _specs_from_config(config)

    whitelist = config.tool_whitelist or [d.name for d in connector.list_tools()]
    guard = _guard_from_config(config)
    if config.output_schema is not None:
        guard.output_schema = config.output_schema
    if config.forbidden_strings:
        guard.forbidden_strings = list(config.forbidden_strings)
    if config.forbidden_patterns:
        guard.forbidden_patterns = list(config.forbidden_patterns)
    if config.rules:
        guard.rules = list(config.rules)

    llm: Any = _llm_from_config(config, whitelist)
    if recorder is not None:
        llm = RecordingLLMAdapter(llm, recorder)
        connector = RecordingMCPConnector(connector, recorder)

    notifier = FakeWebhookNotifier()
    gateway = HumanApprovalGateway(notifier, handler=AutoApprover("approve", "cli auto-approve"))
    tracer = None
    if config.trace_jsonl:
        tracer = Tracer(sink=JsonlSink(config.trace_jsonl, truncate=False))
    return HarnessOrchestrationEngine(
        llm=llm,
        connector=connector,
        output_guard=guard,
        approval_gateway=gateway,
        tool_specs=specs,
        high_risk_tools=set(config.high_risk_tools),
        max_retries=config.max_retries,
        redactor=config.redactor(),
        tracer=tracer,
        logger=StructuredLogger(verbose=config.verbose),
    )


def _cli_responder(
    whitelist: Sequence[str],
) -> Callable[[str, AgentHarnessState, int], LLMResponse]:
    """A deterministic stand-in model for ``harness-core run``.

    It is honest about what it is: a scripted planner, not an LLM. It exists so the
    command is runnable offline; wiring a real model is one adapter away.

    The script mirrors what a competent model would do — read the order first, then
    settle it — so ``run`` exercises the real path: guard → connector → validation.
    """

    def responder(prompt: str, state: AgentHarnessState, attempt: int) -> LLMResponse:
        from .adapters.llm import ToolCallRequest

        tokens = max(1, len(prompt) // 4)
        tool_result = state.execution_result

        if "fetch_order" in whitelist and not isinstance(tool_result, dict):
            return LLMResponse(
                tool_call=ToolCallRequest("fetch_order", {"order_id": "ORD-1001"}),
                raw_text='{"tool_call": {"tool_name": "fetch_order"}}',
                tokens_estimate=tokens,
                model="cli-scripted",
            )

        subtotal = tool_result.get("subtotal", 1200.0) if isinstance(tool_result, dict) else 1200.0
        payload = {
            "order_id": tool_result.get("order_id", "ORD-1001")
            if isinstance(tool_result, dict)
            else "ORD-1001",
            "currency": "CNY",
            "amount": float(subtotal),
            "status": "settled",
        }
        return LLMResponse(
            content=payload,
            raw_text=json.dumps(payload),
            tokens_estimate=tokens,
            model="cli-scripted",
        )

    return responder


def cmd_run(args: argparse.Namespace) -> int:
    try:
        config = HarnessConfig.load(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if args.max_retries is not None:
        config.max_retries = args.max_retries
    if args.trace:
        config.trace_jsonl = args.trace

    recorder = Recorder() if getattr(args, "record", None) else None
    engine = _build_from_config(config, recorder=recorder)
    result = engine.run(
        args.task,
        tool_whitelist=config.tool_whitelist or None,
        max_retries=config.max_retries,
        driver=getattr(args, "driver", "while"),
    )

    print(paint("harness-core run", BOLD))
    print(f"  trace_id : {result.trace_id}")
    print(f"  status   : {status_badge(result.status)}")
    print(f"  driver   : {getattr(args, 'driver', 'while')}")
    print(f"  attempts : {result.attempts}")
    print(f"  duration : {result.duration_ms:.1f} ms")
    print(f"  tokens   : ~{result.tokens_estimate}")
    print("  result   : " + json.dumps(result.execution_result, ensure_ascii=False))
    if result.error_stack is not None:
        print("  error    : " + (result.failure_reason or ""))
        print(result.error_stack.to_prompt_block())
    if args.trace:
        print(f"  jsonl    : {args.trace}")
    if recorder is not None:
        saved = recorder.save(args.record)
        stats = summarize(recorder.to_cassette())
        print(f"  cassette : {saved}  ({stats['llm_calls']} llm, {stats['tool_calls']} tool)")
    return result.exit_code


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


def cmd_replay(args: argparse.Namespace) -> int:
    """Re-run a recorded task against the *real* guards, breaker and HITL."""
    try:
        config = HarnessConfig.load(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    try:
        cassette = Cassette.load(args.cassette)
    except ReplayError as exc:
        print(f"replay error: {exc}", file=sys.stderr)
        return 2

    stats = summarize(cassette)
    if args.inspect:
        print(paint("cassette", BOLD), args.cassette)
        print(f"  entries   : {stats['entries']}")
        print(f"  llm calls : {stats['llm_calls']}")
        print(f"  tool calls: {stats['tool_calls']}")
        print(f"  tools     : {', '.join(stats['tools']) or '(none)'}")
        return 0

    if not args.task:
        print("replay error: --task is required unless --inspect is used", file=sys.stderr)
        return 2

    # The replay must use the *same* contract the recording was validated against,
    # otherwise a tightened config would be silently ignored and a regression would
    # pass. `_guard_from_config` applies output_schema / forbidden content / rules.
    engine = HarnessOrchestrationEngine(
        llm=ReplayLLMAdapter(cassette),
        connector=ReplayMCPConnector(cassette),
        output_guard=_guard_from_config(config),
        tool_specs=_specs_from_config(config),
        high_risk_tools=set(config.high_risk_tools),
        max_retries=config.max_retries,
        logger=StructuredLogger(verbose=config.verbose),
    )
    try:
        result = engine.run(
            args.task,
            tool_whitelist=config.tool_whitelist or None,
            max_retries=config.max_retries,
            driver=getattr(args, "driver", "while"),
        )
    except ReplayError as exc:
        print(f"replay error: {exc}", file=sys.stderr)
        return 1

    print(paint("harness-core replay", BOLD))
    print(f"  cassette : {args.cassette}")
    print(f"  trace_id : {result.trace_id}")
    print(f"  status   : {status_badge(result.status)}")
    print(f"  driver   : {getattr(args, 'driver', 'while')}")
    print(f"  attempts : {result.attempts}")
    print(f"  retries  : {result.state.retry_count if result.state else 0}")
    print("  result   : " + json.dumps(result.execution_result, ensure_ascii=False))
    if result.error_stack is not None:
        print("  error    : " + (result.failure_reason or ""))
        # Mirror `run`: without the findings block a replay failure only shows the
        # breaker code, and the field that actually broke is invisible.
        print(result.error_stack.to_prompt_block())

    if args.expect_status:
        actual = result.status.value
        ok = actual == args.expect_status
        mark = paint("PASS", "\033[32m") if ok else paint("FAIL", "\033[31m")
        print(f"  expect   : [{mark}] {args.expect_status} (got {actual})")
        # With --expect-status the caller has declared the expected outcome, so the
        # exit code reports *whether the expectation held* — not whether the run
        # succeeded. Otherwise `--expect-status FAILED_*` could never exit 0, which
        # is exactly the assertion a regression test needs to make.
        return 0 if ok else 1
    return result.exit_code


# ---------------------------------------------------------------------------
# demo
# ---------------------------------------------------------------------------


def cmd_demo(args: argparse.Namespace) -> int:
    print(paint("═" * 72, BOLD))
    print(paint("  Harness-Agent-Core · offline demo", BOLD))
    print(paint("═" * 72, BOLD))
    print(
        "  Five turns: un-whitelisted tool → invalid arguments → tool ok →\n"
        "  business-rule failure → valid deliverable. Then a circuit-breaker run.\n"
    )

    failures = 0
    for name in demo_scenarios():
        scenario = SCENARIOS[name]
        print(paint(f"▶ scenario: {name}", BOLD))
        print(f"  {scenario.description}")
        outcome = scenario.run()

        for check_name, passed in outcome.checks:
            mark = paint("PASS", "\033[32m") if passed else paint("FAIL", "\033[31m")
            print(f"    [{mark}] {check_name}")
        print(
            f"  → status={status_badge(RunStatus(outcome.status))} "
            f"attempts={outcome.attempts} retries={outcome.retry_count} "
            f"tokens≈{outcome.tokens_estimate} duration={outcome.duration_ms:.1f}ms"
        )
        if outcome.failure_reason:
            print(f"  → reason: {DIM}{outcome.failure_reason}{RESET}")
        if not outcome.passed:
            failures += 1
        print()

    if args.trace:
        sink_path = Path(args.trace)
        sink = JsonlSink(sink_path, truncate=True)
        for name in demo_scenarios():
            outcome = run_scenario(name)
            sink.write(outcome.to_dict())
        print(f"  demo summary written to {sink_path}")

    if failures:
        print(paint(f"{failures} demo scenario(s) failed", "\033[31m"))
        return 1
    print(paint("All demo scenarios behaved as specified.", "\033[32m"))
    return 0


# ---------------------------------------------------------------------------
# eval
# ---------------------------------------------------------------------------


def cmd_eval(args: argparse.Namespace) -> int:
    outcomes = run_all()
    total = len(outcomes)
    passed = sum(1 for outcome in outcomes if outcome.passed)

    # With --json, stdout is reserved for the machine-readable report so a CI job
    # can pipe it straight into jq. The human table goes to stderr instead.
    table_stream = sys.stderr if args.json else sys.stdout

    if args.json:
        payload = {
            "suite": "harness-core builtin",
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "ok": passed == total,
            "scenarios": [outcome.to_dict() for outcome in outcomes],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if args.json == "-":
            print(text)
        else:
            Path(args.json).write_text(text + "\n", encoding="utf-8")
            print(f"report written to {args.json}", file=sys.stderr)

    _print_eval_table(outcomes, table_stream)
    return 0 if passed == total else 1


def _print_eval_table(outcomes: Sequence[Any], stream: Any) -> None:
    total = len(outcomes)
    passed = sum(1 for outcome in outcomes if outcome.passed)
    print(paint("═" * 78, BOLD), file=stream)
    print(paint("  harness-core eval · CI gate", BOLD), file=stream)
    print(paint("═" * 78, BOLD), file=stream)
    print(f"  {'SCENARIO':<28}{'STATUS':<28}{'ATT':>4}{'RETRY':>7}{'TOKENS':>8}", file=stream)
    print("  " + "-" * 74, file=stream)
    for outcome in outcomes:
        mark = paint("✔", "\033[32m") if outcome.passed else paint("✘", "\033[31m")
        print(
            f"  {mark} {outcome.name:<26}{outcome.status:<28}"
            f"{outcome.attempts:>4}{outcome.retry_count:>7}{outcome.tokens_estimate:>8}",
            file=stream,
        )
        if not outcome.passed:
            for check_name, ok in outcome.checks:
                if not ok:
                    print(f"      {paint('failed check:', '\033[31m')} {check_name}", file=stream)
            if outcome.failure_reason:
                print(f"      reason: {outcome.failure_reason}", file=stream)
    print("  " + "-" * 74, file=stream)
    color = "\033[32m" if passed == total else "\033[31m"
    print(f"  {paint(f'{passed}/{total} scenarios passed', color)}", file=stream)
    print(f"  exit code: {0 if passed == total else 1}", file=stream)


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness-core",
        description=(
            "Harness-Agent-Core: a deterministic harness for LLM tool orchestration, "
            "guardrails, self-healing and CI gates."
        ),
    )
    parser.add_argument("--version", action="version", version=f"harness-core {__version__}")
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable ANSI colours (also honours NO_COLOR)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run one task through the harness")
    run_parser.add_argument("task", help="the task prompt")
    run_parser.add_argument("--config", help="YAML/JSON config file")
    run_parser.add_argument("--max-retries", type=int, default=None, help="repair rounds")
    run_parser.add_argument("--trace", help="append the trace record to this JSONL file")
    run_parser.add_argument(
        "--record",
        default=None,
        metavar="CASSETTE",
        help="record the adapter interactions to this cassette (JSONL) for replay",
    )
    run_parser.add_argument(
        "--driver",
        default="while",
        choices=sorted(DRIVERS),
        help="loop driver: 'while' (default), 'graph', or 'langgraph' if installed",
    )
    run_parser.set_defaults(func=cmd_run)

    demo_parser = subparsers.add_parser("demo", help="walk the offline demo scenarios")
    demo_parser.add_argument("--trace", help="write the demo summary to this JSONL file")
    demo_parser.set_defaults(func=cmd_demo)

    eval_parser = subparsers.add_parser("eval", help="run the built-in suite (CI gate)")
    eval_parser.add_argument(
        "--json",
        nargs="?",
        const="-",
        default=None,
        help="write a JSON report ('-' for stdout)",
    )
    eval_parser.set_defaults(func=cmd_eval)

    replay_parser = subparsers.add_parser(
        "replay", help="re-run a recorded task against the real guards"
    )
    replay_parser.add_argument("cassette", help="cassette file written by run --record")
    replay_parser.add_argument("--task", help="the original task prompt")
    replay_parser.add_argument("--config", help="YAML/JSON config file")
    replay_parser.add_argument("--inspect", action="store_true", help="describe the cassette")
    replay_parser.add_argument(
        "--expect-status",
        default=None,
        help="exit 1 unless the replay ends in this status (e.g. SELF_HEALED)",
    )
    replay_parser.add_argument(
        "--driver",
        default="while",
        choices=sorted(DRIVERS),
        help="loop driver to use for the replay",
    )
    replay_parser.set_defaults(func=cmd_replay)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    global _COLOR_ENABLED
    if getattr(args, "no_color", False):
        _COLOR_ENABLED = False

    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary: report, never traceback-dump
        print(f"harness-core: internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        if _env_flag("HARNESS_DEBUG"):
            raise
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
