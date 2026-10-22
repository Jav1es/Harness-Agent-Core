"""Mutation check: break the harness on purpose and prove the tests notice.

"All tests pass" means nothing if the tests are too weak to fail. Each mutant below
disables exactly one safety property; the corresponding test must go red. A mutant
that survives is a hole in the suite, not a passing grade.

Run with::

    python artifacts/mutation_check.py

Exit code 0 only if the baseline is green **and** every mutant is killed.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Mutant:
    name: str
    description: str
    path: str
    old: str
    new: str
    must_fail: str  # the test that is expected to catch it


MUTANTS: list[Mutant] = [
    Mutant(
        name="M1",
        description="whitelist check always passes",
        path="harness_core/guards.py",
        old="        if not self.is_allowed(tool_name):",
        new="        if False:  # MUTANT: whitelist disabled",
        must_fail="tests/test_guards.py::test_whitelist_rejects_unknown_tool",
    ),
    Mutant(
        name="M2",
        description="schema `required` is ignored",
        path="harness_core/guards.py",
        old='        for key in schema.get("required", []):',
        new='        for key in []:  # MUTANT: required disabled',
        must_fail="tests/test_guards.py::test_tool_argument_schema_rejects_missing_required",
    ),
    Mutant(
        name="M3",
        description="fingerprint includes the message (dedup breaks)",
        path="harness_core/errors.py",
        old="                [self.failed_gate, self.tool_name, self.field, self.line, self.severity],",
        new="                [self.failed_gate, self.message, self.severity],  # MUTANT",
        must_fail="tests/test_guards.py::test_fingerprint_is_stable_across_messages",
    ),
    Mutant(
        name="M4",
        description="repeated-failure breaker disabled",
        path="harness_core/circuit_breaker.py",
        old="            if count > self.repeat_threshold:",
        new="            if False:  # MUTANT: repeat breaker disabled",
        must_fail="tests/test_engine.py::test_repeated_failure_trips_earlier_than_the_cap",
    ),
    Mutant(
        name="M5",
        description="retry cap ignored (loops until max_steps)",
        path="harness_core/circuit_breaker.py",
        old="        if attempt >= self.max_retries:",
        new="        if False:  # MUTANT: retry cap disabled",
        must_fail="tests/test_engine.py::test_max_retries_exhausted_returns_circuit_breaker",
    ),
    Mutant(
        name="M6",
        description="high-risk tools execute without approval",
        path="harness_core/engine.py",
        old='            if call.decision == "require_approval":',
        new="            if False:  # MUTANT: HITL bypassed",
        must_fail="tests/test_hitl.py::test_high_risk_call_suspends_and_does_not_execute",
    ),
    Mutant(
        name="M7",
        description="rejection is treated as approval",
        path="harness_core/engine.py",
        old='        if response.decision == "reject":',
        new="        if False:  # MUTANT: reject ignored",
        must_fail="tests/test_hitl.py::test_rejection_stops_the_run_without_executing",
    ),
    Mutant(
        name="M8",
        description="repair block omitted from the next prompt",
        path="harness_core/engine.py",
        old='            lines.append(state.error_stack.to_prompt_block())',
        new='            lines.append("(repair block suppressed)")  # MUTANT',
        must_fail="tests/test_engine.py::test_self_healing_feeds_errors_back_and_succeeds",
    ),
    Mutant(
        name="M9",
        description="WARN findings block the run",
        path="harness_core/engine.py",
        old='        blocking = [f for f in findings if f.severity == "BLOCK"]\n        tracer.end_span(',
        new='        blocking = findings  # MUTANT: WARN treated as BLOCK\n        tracer.end_span(',
        must_fail="tests/test_integration.py::test_non_blocking_severities_never_fail_the_run",
    ),
    Mutant(
        name="M10",
        description="trace sink silently dropped",
        path="harness_core/observability.py",
        old="        if self.sink is not None:\n            self.sink.write(record)",
        new="        pass  # MUTANT: sink disabled",
        must_fail="tests/test_observability.py::test_trace_records_every_required_field",
    ),
    Mutant(
        name="M11",
        description="tool arguments not validated against the schema",
        path="harness_core/guards.py",
        old="        return validate_json_schema(\n            arguments,",
        new="        return []  # MUTANT: argument schema disabled\n        return validate_json_schema(\n            arguments,",
        must_fail="tests/test_guards.py::test_tool_argument_schema_rejects_wrong_type",
    ),
    Mutant(
        name="M12",
        description="successful tool call burns the retry budget",
        path="harness_core/engine.py",
        old="            state.status = RunStatus.RUNNING\n            return state.status",
        new="            state.status = RunStatus.FAILED  # MUTANT: progress counted as failure\n            return state.status",
        must_fail="tests/test_engine.py::test_tool_call_then_deliverable_does_not_burn_retries",
    ),
    Mutant(
        name="M13",
        description="graph driver drops the retry edge",
        path="harness_core/loop.py",
        old='        "retry": "attempt",',
        new='        "retry": None,  # MUTANT: retry terminates instead of looping',
        must_fail="tests/test_loop.py::test_both_drivers_trip_the_breaker_identically",
    ),
    Mutant(
        name="M14",
        description="graph driver miscounts steps (off by one)",
        path="harness_core/loop.py",
        # The anchor must be unique: WhileLoopDriver returns the same expression at
        # the same indentation, so include the preceding line that only the graph
        # driver has.
        old=(
            '                node = None\n'
            '            else:  # pragma: no cover - guards against a typo in EDGES\n'
            '                raise RuntimeError(f"unknown graph node {node!r}")\n'
            '\n'
            '        return LoopOutcome(status=ctx.state.status, steps=step, started_at=started)'
        ),
        new=(
            '                node = None\n'
            '            else:  # pragma: no cover - guards against a typo in EDGES\n'
            '                raise RuntimeError(f"unknown graph node {node!r}")\n'
            '\n'
            '        return LoopOutcome(status=ctx.state.status, steps=step + 1, started_at=started)  # MUTANT'
        ),
        must_fail="tests/test_loop.py::test_graph_driver_reports_the_real_step_count",
    ),
    Mutant(
        name="M15",
        description="pluggable validator result ignored",
        path="harness_core/engine.py",
        old="            if outcome is True or outcome is None:\n                continue",
        new="            continue  # MUTANT: every custom validator passes",
        must_fail="tests/test_engine.py::test_validator_returning_string_uses_it_as_hint",
    ),
    Mutant(
        name="M16",
        description="replay ignores the recorded response (serves a default)",
        path="harness_core/replay.py",
        old="        entry = self._cassette.next_entry(\"llm\")\n        payload = entry.payload.get(\"response\") or {}",
        new="        entry = self._cassette.next_entry(\"llm\")\n        payload = {}  # MUTANT: recorded response discarded",
        must_fail="tests/test_replay.py::test_record_then_replay_reproduces_the_outcome",
    ),
    Mutant(
        name="M17",
        description="exhausted cassette returns an empty result instead of raising",
        path="harness_core/replay.py",
        old="        if cursor >= len(matching):",
        new="        if False:  # MUTANT: exhaustion ignored",
        must_fail="tests/test_replay.py::test_exhausted_cassette_raises_instead_of_defaulting",
    ),
    Mutant(
        name="M18",
        description="replay ignores the configured contract",
        path="harness_core/cli.py",
        old="        output_guard=_guard_from_config(config),",
        new="        output_guard=build_output_guard(),  # MUTANT: config contract dropped",
        must_fail="tests/test_cli_replay.py::test_replay_honours_the_config_contract",
    ),
    Mutant(
        name="M19",
        description="LLM infrastructure errors swallowed as task failures",
        path="harness_core/engine.py",
        old=(
            "        except HarnessInfrastructureError:\n"
            "            # A broken fixture (e.g. an exhausted cassette) is not a task failure.\n"
            "            # Retrying cannot fix it, and dressing it up as a BLOCK finding would\n"
            "            # make a broken test harness look like a plausible model failure.\n"
            "            tracer.end_span(llm_span, \"infrastructure_error\")\n"
            "            raise"
        ),
        new=(
            "        except ZeroDivisionError:  # MUTANT: infrastructure errors no longer escape\n"
            "            tracer.end_span(llm_span, \"infrastructure_error\")\n"
            "            raise"
        ),
        # Must target the LLM boundary. Pointing this at the tool-side test let the
        # mutant survive: the tool boundary has its own handler, so a broken LLM
        # boundary was invisible to it.
        must_fail="tests/test_replay.py::test_exhausted_llm_cassette_escapes_the_self_healing_loop",
    ),
    Mutant(
        name="M20",
        description="tool infrastructure errors swallowed as task failures",
        path="harness_core/engine.py",
        old=(
            "        except HarnessInfrastructureError:\n"
            "            # Same reasoning as the LLM boundary: a broken fixture must not be\n"
            "            # laundered into a tool failure that the model is asked to \"fix\".\n"
            "            tracer.end_span(span, \"infrastructure_error\", tool=call.tool_name)\n"
            "            raise"
        ),
        new=(
            "        except ZeroDivisionError:  # MUTANT: tool-side infra errors swallowed\n"
            "            tracer.end_span(span, \"infrastructure_error\", tool=call.tool_name)\n"
            "            raise"
        ),
        must_fail="tests/test_replay.py::test_replay_of_an_incomplete_cassette_fails_loudly",
    ),
    Mutant(
        name="M21",
        description="--expect-status no longer decides the exit code",
        path="harness_core/cli.py",
        old="        return 0 if ok else 1\n    return result.exit_code",
        new="        return result.exit_code  # MUTANT: expectation ignored\n    return result.exit_code",
        must_fail="tests/test_cli_replay.py::test_expect_status_decides_the_exit_code",
    ),
    Mutant(
        name="M22",
        description="langgraph driver stops retrying (retry routes to END)",
        path="harness_core/loop_langgraph.py",
        old='        if action in ("continue", "retry"):\n            return "attempt"',
        new='        if action == "continue":  # MUTANT: retry falls through to END\n            return "attempt"',
        must_fail="tests/test_loop_langgraph.py::test_langgraph_matches_while",
    ),
    Mutant(
        name="M23",
        description="langgraph driver ignores the step budget",
        path="harness_core/loop_langgraph.py",
        old="        if step >= ctx.max_steps:\n            return {\"step\": step, \"action\": _EXHAUSTED}",
        new="        if False:  # MUTANT: budget ignored\n            return {\"step\": step, \"action\": _EXHAUSTED}",
        must_fail="tests/test_loop_langgraph.py::test_langgraph_respects_max_steps",
    ),
    Mutant(
        name="M24",
        description="langgraph recursion limit dropped (framework default decides)",
        path="harness_core/loop_langgraph.py",
        old='        config: dict[str, Any] = {"recursion_limit": ctx.max_steps * 3 + 10}',
        new='        config: dict[str, Any] = {}  # MUTANT: no explicit limit',
        # Must point at the test that actually observes the config. A behavioural
        # test cannot catch this: langgraph 1.x defaults to 10007, so a realistic run
        # never hits the default and the behaviour looks identical.
        must_fail=(
            "tests/test_loop_langgraph.py::"
            "test_an_explicit_limit_is_what_stops_a_run_not_the_framework"
        ),
    ),
    Mutant(
        name="M25",
        description="optional driver registration crashes when the extra is broken",
        path="harness_core/loop_langgraph.py",
        old="    try:\n        return importlib.util.find_spec(\"langgraph\") is not None\n    except (ImportError, ValueError, AttributeError, OSError):\n        return False",
        new="    return importlib.util.find_spec(\"langgraph\") is not None  # MUTANT: no guard",
        must_fail="tests/test_loop_langgraph.py::test_availability_check_survives_a_broken_find_spec",
    ),
    Mutant(
        name="M26",
        description="MCP tool-level error treated as a transport failure (raises)",
        path="harness_core/adapters/mcp_stdio.py",
        old="            ok=not is_error,",
        new="            ok=True,  # MUTANT: isError ignored",
        must_fail="tests/test_mcp_stdio.py::test_tool_level_error_is_a_normal_outcome",
    ),
    Mutant(
        name="M27",
        description="MCP transport errors downgraded to a tool failure (never raise)",
        path="harness_core/adapters/mcp_stdio.py",
        old="        if pending.error is not None:\n            raise MCPTransportError(",
        new="        if False:  # MUTANT: protocol errors swallowed\n            raise MCPTransportError(",
        must_fail="tests/test_mcp_stdio.py::test_protocol_error_raises",
    ),
    Mutant(
        name="M28",
        description="MCP child process never reaped on close",
        path="harness_core/adapters/mcp_stdio.py",
        old="        if self._closed:\n            return\n        self._closed = True",
        new="        return  # MUTANT: close() is a no-op\n        self._closed = True",
        must_fail="tests/test_mcp_stdio.py::test_close_terminates_the_child_process",
    ),
    Mutant(
        name="M29",
        description="MCP timeout ignored (block forever instead of raising)",
        path="harness_core/adapters/mcp_stdio.py",
        # The deadline is enforced in two places: the pre-check and the queue read.
        # Mutating only the pre-check leaves the queue.Empty branch intact, so the
        # mutant behaves identically — that is a broken mutant, not a weak test.
        # Replace the queue read with an unbounded one to actually remove the guard.
        old="                line = self._lines.get(timeout=remaining)",
        new="                line = self._lines.get()  # MUTANT: no timeout on the read",
        must_fail="tests/test_mcp_stdio.py::test_server_that_never_answers_times_out",
    ),
    Mutant(
        name="M30",
        description="MCP text content not flattened (raw blocks reach the model)",
        path="harness_core/adapters/mcp_stdio.py",
        old="    if texts and not others:\n        return \"\\n\".join(texts)",
        new="    if False:  # MUTANT: content blocks passed through\n        return \"\\n\".join(texts)",
        must_fail="tests/test_mcp_stdio.py::test_calls_a_tool_and_flattens_text_content",
    ),
    Mutant(
        name="M31",
        description="dead transport degrades to an empty toolset (green run, no server)",
        path="harness_core/engine.py",
        old="        except HarnessInfrastructureError:\n            # Discovery failing means the transport is dead. Returning [] here would\n            # make the run look healthy: the whitelist becomes empty, the model is\n            # offered no tools, and a scripted planner can still emit a \"valid\"\n            # deliverable — a green run against a server that never started.\n            raise",
        new="        except ZeroDivisionError:  # MUTANT: dead transport no longer escapes\n            raise",
        must_fail="tests/test_cli_mcp.py::test_tool_discovery_failure_is_not_silently_an_empty_toolset",
    ),
    Mutant(
        name="M32",
        description="unknown mcp_stdio keys silently ignored",
        path="harness_core/cli.py",
        old="        if settings:\n            raise ConfigError(",
        new="        if False:  # MUTANT: typos ignored\n            raise ConfigError(",
        must_fail="tests/test_cli_mcp.py::test_unknown_mcp_stdio_keys_are_rejected",
    ),
    Mutant(
        name="M33",
        description="LLM HTTP error downgraded to a business failure (never raises)",
        path="harness_core/adapters/llm_http.py",
        old="        except urllib.error.HTTPError as exc:\n            detail = _safe_read(exc)\n            raise LLMTransportError(",
        new="        except ZeroDivisionError as exc:  # MUTANT: HTTP errors swallowed\n            detail = _safe_read(exc)\n            raise LLMTransportError(",
        must_fail="tests/test_llm_http.py::test_http_error_raises",
    ),
    Mutant(
        name="M34",
        description="tool_calls ignored (model tool request silently dropped)",
        path="harness_core/adapters/llm_http.py",
        old="    calls = message.get(\"tool_calls\")\n    if not isinstance(calls, list) or not calls:\n        return None",
        new="    calls = None  # MUTANT: tool calls never parsed\n    if not isinstance(calls, list) or not calls:\n        return None",
        must_fail="tests/test_llm_http.py::test_offered_tools_produce_a_tool_call",
    ),
    Mutant(
        name="M35",
        description="api_key accepted in the config file (secret in git)",
        path="harness_core/config.py",
        old="            if \"api_key\" in llm:\n                raise ConfigError(",
        new="            if False:  # MUTANT: plaintext keys allowed in config\n                raise ConfigError(",
        must_fail="tests/test_cli_llm.py::test_api_key_in_the_config_file_is_rejected",
    ),
    Mutant(
        name="M36",
        description="empty api_key_env silently proceeds unauthenticated",
        path="harness_core/cli.py",
        old="    if key_env and not api_key:\n        raise ConfigError(",
        new="    if False:  # MUTANT: missing key ignored\n        raise ConfigError(",
        must_fail="tests/test_cli_llm.py::test_missing_environment_key_fails_loudly",
    ),
    Mutant(
        name="M37",
        description="semantic budget exhaustion silently skips the check (unverified reads as passed)",
        path="harness_core/semantic.py",
        old="            if self.checks_run >= self.max_checks:\n                findings.append(",
        new="            if False:  # MUTANT: budget silently skipped\n                findings.append(",
        must_fail="tests/test_semantic.py::test_budget_exhaustion_blocks_instead_of_skipping",
    ),
    Mutant(
        name="M38",
        description="unparseable judge answer treated as a pass",
        path="harness_core/semantic.py",
        old="        verdict = _parse_verdict(response.content, response.raw_text)\n        if verdict is None:",
        new="        verdict = _parse_verdict(response.content, response.raw_text) or (True, \"\")\n        if verdict is None:",
        must_fail="tests/test_semantic.py::test_unparseable_judge_answer_fails_closed",
    ),
    Mutant(
        name="M39",
        description="semantic token spend not recorded (cost invisible)",
        path="harness_core/semantic.py",
        old="            self.tokens_spent += verdict.tokens",
        new="            pass  # MUTANT: spend not metered",
        must_fail="tests/test_semantic.py::test_tokens_are_accounted",
    ),
    Mutant(
        name="M40",
        description="deterministic failure no longer short-circuits (pays a judge for doomed payloads)",
        path="harness_core/engine.py",
        # The earlier attempt inserted a no-op `if False: pass`, which left the real
        # code path untouched — a broken mutant, not a weak test. Removing the early
        # return is what actually lets the semantic layer run on a payload that has
        # already failed a deterministic gate.
        old="        if blocking:\n            state.fail(ErrorStack(step=\"validate\", findings=findings))\n            return state.status\n\n        # ---- 5b. semantic layer",
        new="        if blocking:\n            state.fail(ErrorStack(step=\"validate\", findings=findings))\n\n        # ---- 5b. semantic layer",
        must_fail="tests/test_semantic.py::test_the_semantic_layer_is_skipped_when_deterministic_checks_fail",
    ),
    Mutant(
        name="M41",
        description="an exploding semantic gate passes silently",
        path="harness_core/semantic.py",
        old="            except Exception as exc:  # noqa: BLE001 - a broken gate must not pass\n                self.checks_run += 1",
        new="            except ZeroDivisionError as exc:  # MUTANT: gate errors swallowed\n                self.checks_run += 1",
        must_fail="tests/test_semantic.py::test_an_exploding_gate_does_not_pass_silently",
    ),
    Mutant(
        name="M42",
        description="injection warnings dropped on the success path (security signal lost)",
        path="harness_core/engine.py",
        old="        state.warnings = [\n            *state.warnings,\n            *(f for f in findings if f.severity != \"BLOCK\"),\n        ]",
        new="        state.warnings = [f for f in findings if f.severity != \"BLOCK\"]  # MUTANT: overwrite",
        must_fail="tests/test_untrusted.py::test_the_injection_is_recorded_as_a_warning_not_a_block",
    ),
    Mutant(
        name="M43",
        description="tool output not fenced as untrusted data (instruction position)",
        path="harness_core/engine.py",
        old="            lines.append(\n                wrap_untrusted(",
        new="            lines.append(\n                (lambda t, source='': t)(",
        must_fail="tests/test_untrusted.py::test_the_next_prompt_fences_the_tool_result",
    ),
    Mutant(
        name="M44",
        description="injection detection escalates to BLOCK (noisy gate gets disabled)",
        path="harness_core/untrusted.py",
        old="            severity=\"WARN\",\n        )\n        for signal in signals",
        new="            severity=\"BLOCK\",  # MUTANT: detection blocks runs\n        )\n        for signal in signals",
        must_fail="tests/test_untrusted.py::test_signals_become_warn_findings",
    ),
    Mutant(
        name="M45",
        description="hex blob threshold raised past a real injection length",
        path="harness_core/untrusted.py",
        old="    for match in re.finditer(r\"\\b[0-9a-fA-F]{40,}\\b\", text):",
        new="    for match in re.finditer(r\"\\b[0-9a-fA-F]{80,}\\b\", text):  # MUTANT: misses short payloads",
        must_fail="tests/test_untrusted.py::test_the_shortest_useful_injection_is_caught_when_hex_encoded",
    ),
    Mutant(
        name="M46",
        description="tool output never scanned for injection signals",
        path="harness_core/engine.py",
        old="            signals = scan_payload(result.content)",
        new="            signals = []  # MUTANT: tool output trusted blindly",
        must_fail="tests/test_untrusted.py::test_detection_is_recorded_in_state_metadata",
    ),
    Mutant(
        name="M47",
        description="cassette version never checked (future file parsed as current)",
        path="harness_core/replay.py",
        old="        if version != CASSETTE_VERSION:",
        new="        if False:  # MUTANT: version ignored",
        must_fail="tests/test_cassette_version.py::test_a_future_version_is_refused_not_guessed",
    ),
    Mutant(
        name="M48",
        description="malformed cassette entry skipped instead of reported",
        path="harness_core/replay.py",
        old="                raise ReplayError(\n                    f\"{target}:{offset}: entry without 'kind': {stripped[:120]!r}\"\n                )",
        new="                continue  # MUTANT: bad entry silently skipped",
        must_fail="tests/test_cassette_version.py::test_an_entry_without_kind_is_reported",
    ),
    Mutant(
        name="M49",
        description="legacy cassette loaded without migrating (stays v0 on disk)",
        path="harness_core/replay.py",
        old="            version = migrate(target, version)",
        new="            pass  # MUTANT: legacy file never upgraded",
        must_fail="tests/test_cassette_version.py::test_a_headerless_cassette_is_migrated_in_place",
    ),
    Mutant(
        name="M50",
        description="migration gap invented instead of reported",
        path="harness_core/replay.py",
        old="        if step is None:\n            raise CassetteVersionError(",
        new="        if step is None:\n            break  # MUTANT: silently stops migrating\n        if False:\n            raise CassetteVersionError(",
        must_fail="tests/test_cassette_version.py::test_a_missing_migration_step_is_reported",
    ),
    Mutant(
        name="M51",
        description="migration drops entry order",
        path="harness_core/replay.py",
        old="    return {\"cassette_version\": 1, \"entries\": list(document.get(\"entries\") or [])}",
        new="    return {\"cassette_version\": 1, \"entries\": list(reversed(document.get(\"entries\") or []))}  # MUTANT",
        must_fail="tests/test_cassette_version.py::test_migration_preserves_entry_order",
    ),
    Mutant(
        name="M52",
        description="HTTP MCP session id never echoed (later requests unauthenticated)",
        path="harness_core/adapters/mcp_http.py",
        old="        if self._session_id:\n            headers[\"Mcp-Session-Id\"] = self._session_id",
        new="        if False:  # MUTANT: session id dropped\n            headers[\"Mcp-Session-Id\"] = self._session_id",
        must_fail="tests/test_mcp_http.py::test_the_session_id_is_echoed_on_later_requests",
    ),
    Mutant(
        name="M53",
        description="HTTP MCP tool-level error treated as success",
        path="harness_core/adapters/mcp_http.py",
        old="            ok=not is_error,",
        new="            ok=True,  # MUTANT: isError ignored",
        must_fail="tests/test_mcp_http.py::test_a_tool_level_failure_is_a_normal_outcome",
    ),
    Mutant(
        name="M54",
        description="HTTP MCP JSON-RPC error swallowed as an empty result",
        path="harness_core/adapters/mcp_http.py",
        old="                if error:\n                    raise MCPHttpError(",
        new="                if False:  # MUTANT: protocol errors ignored\n                    raise MCPHttpError(",
        must_fail="tests/test_mcp_http.py::test_a_jsonrpc_error_raises",
    ),
    Mutant(
        name="M55",
        description="SSE data: prefix not stripped (every message lost)",
        path="harness_core/adapters/mcp_http.py",
        # Repointed twice: first at the whole-body parser, then at the streaming
        # reader. Both were correct mutations of *their* path, but after the two
        # parsers were merged into `_sse_line_to_message` there is only one place
        # left — which is the point of merging them.
        old="    payload = stripped[len(\"data:\") :].strip()",
        new="    payload = stripped  # MUTANT: prefix left on the line",
        must_fail="tests/test_mcp_http.py::test_sse_parser_strips_the_data_prefix",
    ),
    Mutant(
        name="M56",
        description="HTTP MCP session never released on close (server state leaks)",
        path="harness_core/adapters/mcp_http.py",
        old="        try:\n            self._send_http(\"DELETE\", None)\n        except MCPHttpError:\n            pass  # best effort: closing must not raise",
        new="        pass  # MUTANT: session never released",
        must_fail="tests/test_mcp_http.py::test_close_releases_the_session_on_the_server",
    ),
    Mutant(
        name="M57",
        description="both MCP transports configured silently accepted",
        path="harness_core/config.py",
        old="        if mcp_stdio and mcp_http:",
        new="        if False:  # MUTANT: conflicting transports allowed",
        must_fail="tests/test_cli_mcp_http.py::test_configuring_both_transports_is_rejected",
    ),
    Mutant(
        name="M58",
        description="missing MCP token env silently proceeds unauthenticated",
        path="harness_core/cli.py",
        old="            if not token:\n                raise ConfigError(",
        new="            if False:  # MUTANT: missing token ignored\n                raise ConfigError(",
        must_fail="tests/test_cli_mcp_http.py::test_a_missing_token_env_fails_loudly",
    ),
    Mutant(
        name="M59",
        description="snapshot version never checked (future format parsed as current)",
        path="harness_core/durable.py",
        old="        if int(version) > SNAPSHOT_VERSION:",
        new="        if False:  # MUTANT: version ignored",
        must_fail="tests/test_durable.py::test_a_future_snapshot_is_refused",
    ),
    Mutant(
        name="M60",
        description="restore returns a new object instead of mutating the live state",
        path="harness_core/durable.py",
        old="    rebuilt = snapshot.state\n    target.task_prompt = rebuilt.task_prompt",
        new="    rebuilt = snapshot.state\n    target = AgentHarnessState(task_prompt=rebuilt.task_prompt, tool_whitelist=[])\n    target.task_prompt = rebuilt.task_prompt",
        must_fail="tests/test_durable.py::test_restore_mutates_the_target_object",
    ),
    Mutant(
        name="M61",
        description="resume drops the trace id (resumed half looks like a new run)",
        path="harness_core/durable.py",
        old="    target.trace_id = rebuilt.trace_id",
        new="    pass  # MUTANT: trace id not carried over",
        must_fail="tests/test_durable.py::test_restore_keeps_the_trace_id",
    ),
    Mutant(
        name="M62",
        description="unknown status silently becomes PENDING (finished run looks resumable)",
        path="harness_core/state.py",
        old="            raise ValueError(f\"unknown run status in snapshot: {raw_status!r}\") from None",
        new="            status = RunStatus.PENDING  # MUTANT: unknown status guessed",
        must_fail="tests/test_durable.py::test_a_corrupt_state_is_reported_as_a_resume_error",
    ),
    Mutant(
        name="M63",
        description="durable graph carries the live ctx (msgpack rejects the checkpoint)",
        path="harness_core/loop_langgraph.py",
        old="            initial = {\n                \"snapshot\": _snapshot_of_ctx(ctx, step=0, action=\"start\"),",
        new="            initial = {\n                \"ctx\": ctx,\n                \"snapshot\": _snapshot_of_ctx(ctx, step=0, action=\"start\"),",
        must_fail="tests/test_durable.py::test_a_durable_walk_does_not_serialise_the_context",
    ),
    Mutant(
        name="M64",
        description="get_driver rejects a configured driver instance (checkpointer dropped)",
        path="harness_core/loop.py",
        old="    if not isinstance(name, str):\n        if not hasattr(name, \"drive\"):",
        new="    if False:  # MUTANT: instances rejected\n        if not hasattr(name, \"drive\"):",
        must_fail="tests/test_durable.py::test_a_durable_langgraph_walk_persists_a_snapshot",
    ),
    Mutant(
        name="M65",
        description="the original payload is stored instead of the redacted one",
        path="harness_core/engine.py",
        old="        state.execution_result = payload",
        new="        state.execution_result = response.content  # MUTANT: secret kept",
        must_fail="tests/test_redact.py::test_the_deliverable_is_the_redacted_payload",
    ),
    Mutant(
        name="M66",
        description="redaction runs after the forbidden-pattern gate (blocks on a removed string)",
        path="harness_core/engine.py",
        old="        findings = [*redaction_findings, *self.validate_output(payload)]",
        new="        findings = [*redaction_findings, *self.validate_output(response.content)]",
        must_fail="tests/test_redact.py::test_redaction_happens_before_the_forbidden_pattern_gate",
    ),
    Mutant(
        name="M67",
        description="redaction findings dropped from the success path (leak invisible)",
        path="harness_core/engine.py",
        # The first attempt removed a `state.warnings` append in the redaction block,
        # which survived because the same findings were *also* reaching warnings via
        # the `findings` list. That duplication was a real bug; the mutant now targets
        # the only remaining path.
        old="            *(f for f in findings if f.severity != \"BLOCK\"),",
        new="            *(),  # MUTANT: warnings dropped",
        must_fail="tests/test_redact.py::test_redaction_is_reported_as_a_warning",
    ),
    Mutant(
        name="M68",
        description="Luhn check bypassed (every 16-digit id redacted)",
        path="harness_core/redact.py",
        old="    return 13 <= len(digits) <= 19 and digits.isdigit() and _luhn_ok(digits)",
        new="    return 13 <= len(digits) <= 19 and digits.isdigit()  # MUTANT: no Luhn",
        must_fail="tests/test_redact.py::test_a_luhn_invalid_number_is_not_redacted",
    ),
    Mutant(
        name="M69",
        description="redaction findings become BLOCK (a handled leak blocks the run)",
        path="harness_core/redact.py",
        old='            severity="WARN",',
        new='            severity="BLOCK",  # MUTANT: handled leak blocks',
        must_fail="tests/test_redact.py::test_redaction_is_a_warning",
    ),
    Mutant(
        name="M70",
        description="nested payloads not walked (secrets in sub-objects survive)",
        path="harness_core/redact.py",
        old="            if isinstance(value, dict):",
        new="            if False:  # MUTANT: nested dicts skipped",
        must_fail="tests/test_redact.py::test_nested_payloads_are_walked",
    ),
    Mutant(
        name="M71",
        description="field scoping ignored (redacts outside the configured fields)",
        path="harness_core/redact.py",
        old="            if self.fields is not None and not _path_selected(path, self.fields):",
        new="            if False:  # MUTANT: field scope ignored",
        must_fail="tests/test_redact.py::test_field_scoping_limits_the_walk",
    ),
    Mutant(
        name="M72",
        description="SSE stream read to EOF instead of stopping at the reply",
        path="harness_core/adapters/mcp_http.py",
        # Anchor moved when the two SSE parsers were merged; the loop that decides
        # *when to stop* is still this one.
        old="        if message is not None and message.get(\"id\") == want_id:\n            return \"\".join(lines), message",
        new="        if False:  # MUTANT: never stop early\n            return \"\".join(lines), message",
        must_fail="tests/test_mcp_http_stream.py::test_a_held_open_stream_returns_before_it_closes",
    ),
    Mutant(
        name="M73",
        description="streaming parser does not consume the reply it found (falls back to empty)",
        path="harness_core/adapters/mcp_http.py",
        old="                if want_id is not None and \"text/event-stream\" in content_type.lower():",
        new="                if False:  # MUTANT: always read whole body",
        must_fail="tests/test_mcp_http_stream.py::test_a_held_open_stream_returns_before_it_closes",
    ),
    Mutant(
        name="M74",
        description="streamed reply body discarded (fallback has nothing to parse)",
        path="harness_core/adapters/mcp_http.py",
        # The first attempt skipped the `matched` branch, which survived because the
        # fallback re-parsed the same bytes and found the same message — the
        # behaviour did not change. This version drops the body the stream produced,
        # so the fallback genuinely has nothing and the request must fail.
        old="                    body, matched = _read_sse_until(response, want_id)",
        new="                    body, matched = \"\", None  # MUTANT: stream discarded",
        must_fail="tests/test_mcp_http_stream.py::test_a_held_open_stream_returns_a_correct_handshake",
    ),
    Mutant(
        name="M75",
        description="SSE reader keeps consuming after finding the reply",
        path="harness_core/adapters/mcp_http.py",
        old="        if message is not None and message.get(\"id\") == want_id:\n            return \"\".join(lines), message",
        new="        if message is not None and message.get(\"id\") == want_id:\n            pass  # MUTANT: keep reading",
        must_fail="tests/test_mcp_http_stream.py::test_read_sse_stops_at_the_matching_id",
    ),
    Mutant(
        name="M76",
        description="jsonrpc error in a streamed reply treated as success",
        path="harness_core/adapters/mcp_http.py",
        old="            error = matched.get(\"error\")\n            if error:",
        new="            error = matched.get(\"error\")\n            if False:  # MUTANT: streamed errors ignored",
        must_fail="tests/test_mcp_http_stream.py::test_a_jsonrpc_error_over_a_held_stream_still_raises",
    ),
    Mutant(
        name="M77",
        description="interrupt_mode accepted without a checkpointer",
        path="harness_core/loop_langgraph.py",
        old="        if interrupt_mode and checkpointer is None:",
        new="        if False:  # MUTANT: no checkpointer required",
        must_fail="tests/test_langgraph_interrupt.py::test_interrupt_mode_requires_a_checkpointer",
    ),
    Mutant(
        name="M78",
        description="model turn re-runs on resume (extra LLM call, possibly a different call)",
        path="harness_core/loop_langgraph.py",
        old="        resuming = self.interrupt_mode and _awaiting_decision(ctx.state)",
        new="        resuming = False  # MUTANT: always re-run the model turn",
        must_fail="tests/test_langgraph_interrupt.py::test_approval_does_not_re_run_the_earlier_steps",
    ),
    Mutant(
        name="M79",
        description="approved tool never executed (approval authorises nothing)",
        path="harness_core/loop_langgraph.py",
        old="        result = ctx.engine.execute_tool_call(ctx.state, call)",
        new="        result = None  # MUTANT: approved tool not run\n        if result is None:\n            ctx.state.status = RunStatus.RUNNING\n            return \"continue\"\n        result = ctx.engine.execute_tool_call(ctx.state, call)",
        must_fail="tests/test_langgraph_interrupt.py::test_approval_runs_the_tool_exactly_once",
    ),
    Mutant(
        name="M80",
        description="non-committal answer treated as approval",
        path="harness_core/loop_langgraph.py",
        old="    if isinstance(answer, str):\n        return answer.strip().lower() in _APPROVAL_WORDS",
        new="    if isinstance(answer, str):\n        return True  # MUTANT: any answer approves",
        must_fail="tests/test_langgraph_interrupt.py::test_a_noncommittal_answer_is_a_rejection",
    ),
    Mutant(
        name="M81",
        description="rejection still runs the tool",
        path="harness_core/loop_langgraph.py",
        old="        if not approved:\n            ctx.state.status = RunStatus.REJECTED_BY_HUMAN",
        new="        if False:  # MUTANT: rejection ignored\n            ctx.state.status = RunStatus.REJECTED_BY_HUMAN",
        must_fail="tests/test_langgraph_interrupt.py::test_rejection_ends_the_run",
    ),
    Mutant(
        name="M82",
        description="awaiting-decision check ignores the missing decision (timeout resumes as answered)",
        path="harness_core/loop_langgraph.py",
        old="        and state.approval_decision is None",
        new="        # MUTANT: decision state ignored",
        # Pointed at the wrong test first: `test_the_tool_has_not_run_while_suspended`
        # passes either way, because the tool does not run while parked regardless of
        # how "parked" is detected. The behaviour this condition controls is whether a
        # *timed-out* gateway counts as still-waiting — so that is what must fail.
        must_fail=(
            "tests/test_langgraph_interrupt.py"
            "::test_a_gateway_timeout_is_not_treated_as_awaiting_a_decision"
        ),
    ),
]


def run_pytest(path: str, cwd: Path, timeout: float = 60.0) -> int:
    """Return the pytest exit code; a hang counts as "caught" (it is still a failure).

    The timeout matters: a mutant that removes a loop bound would otherwise hang the
    checker instead of being reported. A hang is a *detected* mutant, so it maps to a
    non-zero code.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-x", path],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124
    return result.returncode


def main() -> int:
    print("baseline: ", end="", flush=True)
    if run_pytest("tests", ROOT) != 0:
        print("FAILED — fix the suite before mutating anything")
        return 1
    print("green")

    failures = 0
    survivors: list[str] = []
    skipped: list[str] = []

    for mutant in MUTANTS:
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir) / "repo"
            shutil.copytree(
                ROOT,
                work,
                ignore=shutil.ignore_patterns(
                    "__pycache__", ".pytest_cache", "*.egg-info", ".git", "artifacts"
                ),
            )
            target = work / mutant.path
            original = target.read_text(encoding="utf-8")
            if mutant.old not in original:
                print(f"{mutant.name:<5} SKIP — anchor not found in {mutant.path}")
                skipped.append(mutant.name)
                failures += 1
                continue
            target.write_text(original.replace(mutant.old, mutant.new, 1), encoding="utf-8")

            caught = run_pytest(mutant.must_fail, work) != 0
            if caught:
                print(f"{mutant.name:<5} KILLED  {mutant.description}")
            else:
                print(f"{mutant.name:<5} ALIVE   {mutant.description}  <-- test is too weak")
                survivors.append(mutant.name)
                failures += 1

    print()
    total = len(MUTANTS)
    killed = total - len(survivors) - len(skipped)
    # Report skipped mutants explicitly. Folding them into "N/N killed" would claim
    # coverage that was never tested: a mutant whose anchor drifted is a mutant that
    # proved nothing, and the summary is the line people actually read.
    print(f"{killed}/{total} mutants killed")
    if skipped:
        print(f"SKIPPED (anchor drifted — not a pass): {', '.join(skipped)}")
    if survivors:
        print("survivors:", ", ".join(survivors))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
