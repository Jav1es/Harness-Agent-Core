"""Configuring a real MCP server through the CLI.

The CLI's planner is scripted, so `run` will not call an arbitrary MCP tool — that
is by design (see the README's known limitations). What these tests pin down is the
*wiring*: a config with `mcp_stdio` must produce a real `StdioMCPConnector` that
talks to the configured server, and a bad config must fail before any process is
spawned.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from harness_core import cli
from harness_core.adapters.mcp import FakeMCPConnector
from harness_core.adapters.mcp_stdio import StdioMCPConnector
from harness_core.config import ConfigError, HarnessConfig

STUB = str(Path(__file__).parent / "mcp_server_stub.py")


def _config(tmp_path: Path, **overrides) -> Path:
    data = {
        "mcp_stdio": {"command": sys.executable, "args": [STUB], "timeout": 15},
        "tool_whitelist": ["echo"],
    }
    data.update(overrides)
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# config parsing
# ---------------------------------------------------------------------------


def test_mcp_stdio_block_is_parsed(tmp_path: Path) -> None:
    config = HarnessConfig.load(str(_config(tmp_path)))
    assert config.mcp_stdio is not None
    assert config.mcp_stdio["command"] == sys.executable


def test_mcp_stdio_requires_a_command(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"mcp_stdio": {"args": ["x"]}}), encoding="utf-8")
    with pytest.raises(ConfigError, match="command"):
        HarnessConfig.load(str(path))


def test_mcp_stdio_must_be_a_mapping(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"mcp_stdio": ["python"]}), encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        HarnessConfig.load(str(path))


def test_mcp_stdio_round_trips_through_to_dict(tmp_path: Path) -> None:
    config = HarnessConfig.load(str(_config(tmp_path)))
    again = HarnessConfig.from_dict(config.to_dict())
    assert again.mcp_stdio == config.mcp_stdio


# ---------------------------------------------------------------------------
# connector selection
# ---------------------------------------------------------------------------


def test_configured_server_produces_a_real_connector(tmp_path: Path) -> None:
    config = HarnessConfig.load(str(_config(tmp_path)))
    conn = cli._connector_from_config(config)
    try:
        assert isinstance(conn, StdioMCPConnector)
        assert not isinstance(conn, FakeMCPConnector)
        # Prove it is live: it must actually list the stub's tools.
        assert [t.name for t in conn.list_tools()] == ["echo", "add", "fail", "boom"]
    finally:
        conn.close()


def test_no_config_falls_back_to_the_fake_connector() -> None:
    """A fresh machine must still run: no server configured, no server needed."""
    conn = cli._connector_from_config(HarnessConfig())
    assert isinstance(conn, FakeMCPConnector)


def test_unknown_mcp_stdio_keys_are_rejected(tmp_path: Path) -> None:
    """A typo like 'timout' must fail loudly, not be silently ignored."""
    path = _config(tmp_path, mcp_stdio={"command": sys.executable, "timout": 5})
    config = HarnessConfig.load(str(path))
    with pytest.raises(ConfigError, match="unknown mcp_stdio keys"):
        cli._connector_from_config(config)


def test_timeout_and_env_are_forwarded(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        mcp_stdio={
            "command": sys.executable,
            "args": [STUB],
            "timeout": 3.5,
            "env": {"HARNESS_TEST_FLAG": "1"},
        },
    )
    config = HarnessConfig.load(str(path))
    conn = cli._connector_from_config(config)
    try:
        assert isinstance(conn, StdioMCPConnector)
        assert conn.timeout == 3.5
        assert conn.env == {"HARNESS_TEST_FLAG": "1"}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# the CLI still runs end to end with a server configured
# ---------------------------------------------------------------------------


def test_cli_run_works_with_a_configured_server(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _config(tmp_path)
    code = cli.main(["run", "Settle order ORD-1001", "--config", str(path)])
    assert code == 0
    assert "FIRST_PASS" in capsys.readouterr().out


def test_cli_run_with_a_broken_server_reports_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dead server must not produce a fake success."""
    path = _config(
        tmp_path, mcp_stdio={"command": "definitely-not-a-real-binary-xyz", "timeout": 2}
    )
    code = cli.main(["run", "Settle order", "--config", str(path)])
    assert code == 1
    err = capsys.readouterr().err
    assert "internal error" in err or "could not start" in err


def test_tool_discovery_failure_is_not_silently_an_empty_toolset() -> None:
    """Regression: returning [] on a dead transport produced a green run.

    The failure mode was subtle and dangerous. `_discover_tools` caught everything
    and returned an empty list, so the whitelist became empty, the model was offered
    no tools, and the scripted planner still emitted a valid deliverable —
    `FIRST_PASS` against a server that never started. Only the log line hinted at it.

    Infrastructure errors must escape; ordinary connector errors may still degrade.
    """
    from harness_core.adapters.llm import FakeLLMAdapter
    from harness_core.engine import HarnessOrchestrationEngine
    from harness_core.errors import HarnessInfrastructureError
    from harness_core.observability import StructuredLogger

    class DeadConnector:
        name = "dead"

        def list_tools(self):
            raise HarnessInfrastructureError("could not start MCP server")

        def call_tool(self, tool_name, arguments):
            raise HarnessInfrastructureError("could not start MCP server")

        def close(self):
            return None

    engine = HarnessOrchestrationEngine(
        llm=FakeLLMAdapter([]),
        connector=DeadConnector(),
        logger=StructuredLogger(enabled=False),
    )
    with pytest.raises(HarnessInfrastructureError):
        engine.run("Settle order")


def test_ordinary_connector_errors_still_degrade_to_an_empty_toolset() -> None:
    """A non-infrastructure discovery error stays non-fatal, as before."""
    from harness_core.adapters.llm import FakeLLMAdapter
    from harness_core.engine import HarnessOrchestrationEngine
    from harness_core.observability import StructuredLogger

    from .helpers import response_for

    class FlakyConnector:
        name = "flaky"

        def list_tools(self):
            raise RuntimeError("transient discovery glitch")

        def call_tool(self, tool_name, arguments):
            return None

        def close(self):
            return None

    engine = HarnessOrchestrationEngine(
        llm=FakeLLMAdapter([response_for()]),
        connector=FlakyConnector(),
        logger=StructuredLogger(enabled=False),
    )
    result = engine.run("Settle order", tool_whitelist=["fetch_order"])
    assert result.ok, "a transient discovery glitch must not abort the whole run"
