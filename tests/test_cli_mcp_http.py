"""Selecting the HTTP/SSE MCP transport through config."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import pytest

from harness_core import cli
from harness_core.adapters.mcp import FakeMCPConnector
from harness_core.adapters.mcp_http import HttpMCPConnector
from harness_core.adapters.mcp_stdio import StdioMCPConnector
from harness_core.config import ConfigError, HarnessConfig

STUB = str(Path(__file__).parent / "mcp_http_server_stub.py")


class StubServer:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, STUB],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.port = self._wait()

    def _wait(self, timeout: float = 15.0) -> int:
        assert self.proc.stdout is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                break
            if line.startswith("PORT "):
                return int(line.split()[1])
        raise RuntimeError("stub server did not report a port")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)


@pytest.fixture
def server() -> Iterator[StubServer]:
    srv = StubServer()
    try:
        yield srv
    finally:
        srv.close()


def _config(tmp_path: Path, **data) -> Path:
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def test_mcp_http_block_is_parsed(tmp_path: Path) -> None:
    config = HarnessConfig.load(str(_config(tmp_path, mcp_http={"url": "http://h/mcp"})))
    assert config.mcp_http == {"url": "http://h/mcp"}


def test_mcp_http_requires_a_url(tmp_path: Path) -> None:
    path = _config(tmp_path, mcp_http={"timeout": 5})
    with pytest.raises(ConfigError, match="url"):
        HarnessConfig.load(str(path))


def test_mcp_http_must_be_a_mapping(tmp_path: Path) -> None:
    path = _config(tmp_path, mcp_http=["http://h/mcp"])
    with pytest.raises(ConfigError, match="mapping"):
        HarnessConfig.load(str(path))


def test_headers_must_be_a_mapping(tmp_path: Path) -> None:
    path = _config(tmp_path, mcp_http={"url": "http://h/mcp", "headers": ["x"]})
    with pytest.raises(ConfigError, match="headers"):
        HarnessConfig.load(str(path))


def test_configuring_both_transports_is_rejected(tmp_path: Path) -> None:
    """Two transports cannot both be the connector; picking one silently is worse."""
    path = _config(
        tmp_path,
        mcp_stdio={"command": "python"},
        mcp_http={"url": "http://h/mcp"},
    )
    with pytest.raises(ConfigError, match="only one of"):
        HarnessConfig.load(str(path))


def test_mcp_http_round_trips_through_to_dict(tmp_path: Path) -> None:
    config = HarnessConfig.load(str(_config(tmp_path, mcp_http={"url": "http://h/mcp"})))
    again = HarnessConfig.from_dict(config.to_dict())
    assert again.mcp_http == config.mcp_http


# ---------------------------------------------------------------------------
# connector selection
# ---------------------------------------------------------------------------


def test_http_config_produces_an_http_connector(tmp_path: Path) -> None:
    config = HarnessConfig.load(str(_config(tmp_path, mcp_http={"url": "http://h/mcp"})))
    conn = cli._connector_from_config(config)
    assert isinstance(conn, HttpMCPConnector)
    assert conn.url == "http://h/mcp"


def test_http_config_wins_over_the_fake(tmp_path: Path) -> None:
    config = HarnessConfig.load(str(_config(tmp_path, mcp_http={"url": "http://h/mcp"})))
    assert not isinstance(cli._connector_from_config(config), FakeMCPConnector)


def test_stdio_config_still_selects_stdio(tmp_path: Path) -> None:
    config = HarnessConfig.load(
        str(_config(tmp_path, mcp_stdio={"command": sys.executable}))
    )
    conn = cli._connector_from_config(config)
    try:
        assert isinstance(conn, StdioMCPConnector)
    finally:
        conn.close()


def test_no_config_still_falls_back_to_the_fake() -> None:
    assert isinstance(cli._connector_from_config(HarnessConfig()), FakeMCPConnector)


def test_unknown_mcp_http_keys_are_rejected(tmp_path: Path) -> None:
    config = HarnessConfig.load(
        str(_config(tmp_path, mcp_http={"url": "http://h/mcp", "timout": 5}))
    )
    with pytest.raises(ConfigError, match="unknown mcp_http keys"):
        cli._connector_from_config(config)


# ---------------------------------------------------------------------------
# tokens come from the environment, never the config file
# ---------------------------------------------------------------------------


def test_token_env_is_read_into_an_authorization_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_MCP_TOKEN", "secret-token")
    config = HarnessConfig.load(
        str(_config(tmp_path, mcp_http={"url": "http://h/mcp", "token_env": "HARNESS_MCP_TOKEN"}))
    )
    conn = cli._connector_from_config(config)
    assert isinstance(conn, HttpMCPConnector)
    assert conn.headers["Authorization"] == "Bearer secret-token"


def test_a_missing_token_env_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HARNESS_MCP_TOKEN", raising=False)
    config = HarnessConfig.load(
        str(_config(tmp_path, mcp_http={"url": "http://h/mcp", "token_env": "HARNESS_MCP_TOKEN"}))
    )
    with pytest.raises(ConfigError, match="empty"):
        cli._connector_from_config(config)


def test_no_token_env_means_no_authorization_header(tmp_path: Path) -> None:
    config = HarnessConfig.load(str(_config(tmp_path, mcp_http={"url": "http://h/mcp"})))
    conn = cli._connector_from_config(config)
    assert isinstance(conn, HttpMCPConnector)
    assert "Authorization" not in conn.headers


# ---------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------


def test_cli_run_against_a_real_http_mcp_server(
    tmp_path: Path, server: StubServer, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _config(tmp_path, mcp_http={"url": server.url, "timeout": 10})
    code = cli.main(["run", "Settle order ORD-1001", "--config", str(path)])
    assert code == 0
    assert "FIRST_PASS" in capsys.readouterr().out


def test_cli_run_with_a_dead_http_server_reports_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _config(tmp_path, mcp_http={"url": "http://127.0.0.1:1/mcp", "timeout": 2})
    code = cli.main(["run", "Settle order", "--config", str(path)])
    assert code == 1
    err = capsys.readouterr().err
    assert "internal error" in err or "could not reach" in err
