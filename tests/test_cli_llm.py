"""Configuring a real LLM endpoint through the CLI."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import pytest

from harness_core import cli
from harness_core.adapters.llm import FakeLLMAdapter
from harness_core.adapters.llm_http import HttpLLMAdapter
from harness_core.config import ConfigError, HarnessConfig

STUB = str(Path(__file__).parent / "llm_server_stub.py")


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
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

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


def _config(tmp_path: Path, base_url: str, **llm_extra) -> Path:
    llm = {"base_url": base_url, "model": "stub-model", "timeout": 10}
    llm.update(llm_extra)
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"llm": llm}), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# config parsing
# ---------------------------------------------------------------------------


def test_llm_block_is_parsed(tmp_path: Path) -> None:
    config = HarnessConfig.load(str(_config(tmp_path, "http://h/v1")))
    assert config.llm is not None
    assert config.llm["model"] == "stub-model"


def test_llm_requires_base_url_and_model(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"llm": {"base_url": "http://h/v1"}}), encoding="utf-8")
    with pytest.raises(ConfigError, match="base_url"):
        HarnessConfig.load(str(path))


def test_api_key_in_the_config_file_is_rejected(tmp_path: Path) -> None:
    """A config file ends up in git; a key in it is a leaked secret."""
    path = tmp_path / "leak.json"
    path.write_text(
        json.dumps({"llm": {"base_url": "http://h/v1", "model": "m", "api_key": "sk-123"}}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="api_key"):
        HarnessConfig.load(str(path))


def test_llm_round_trips_through_to_dict(tmp_path: Path) -> None:
    config = HarnessConfig.load(str(_config(tmp_path, "http://h/v1")))
    assert HarnessConfig.from_dict(config.to_dict()).llm == config.llm


# ---------------------------------------------------------------------------
# adapter selection
# ---------------------------------------------------------------------------


def test_no_llm_config_uses_the_scripted_planner() -> None:
    adapter = cli._llm_from_config(HarnessConfig(), ["fetch_order"])
    assert isinstance(adapter, FakeLLMAdapter)


def test_configured_endpoint_produces_a_real_adapter(tmp_path: Path, server: StubServer) -> None:
    config = HarnessConfig.load(str(_config(tmp_path, server.base_url)))
    adapter = cli._llm_from_config(config, [])
    assert isinstance(adapter, HttpLLMAdapter)
    assert adapter.model == "stub-model"


def test_api_key_is_read_from_the_environment(
    tmp_path: Path, server: StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_TEST_KEY", "sk-from-env")
    config = HarnessConfig.load(
        str(_config(tmp_path, server.base_url, api_key_env="HARNESS_TEST_KEY"))
    )
    adapter = cli._llm_from_config(config, [])
    assert isinstance(adapter, HttpLLMAdapter)
    assert adapter.headers()["Authorization"] == "Bearer sk-from-env"


def test_missing_environment_key_fails_loudly(
    tmp_path: Path, server: StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty key must stop the run, not silently send unauthenticated requests."""
    monkeypatch.delenv("HARNESS_TEST_KEY", raising=False)
    config = HarnessConfig.load(
        str(_config(tmp_path, server.base_url, api_key_env="HARNESS_TEST_KEY"))
    )
    with pytest.raises(ConfigError, match="empty"):
        cli._llm_from_config(config, [])


def test_unknown_llm_keys_are_rejected(tmp_path: Path) -> None:
    config = HarnessConfig.load(
        str(_config(tmp_path, "http://h/v1", timout=5))
    )
    with pytest.raises(ConfigError, match="unknown llm keys"):
        cli._llm_from_config(config, [])


# ---------------------------------------------------------------------------
# the CLI runs end to end against a real endpoint
# ---------------------------------------------------------------------------


def test_cli_run_with_a_real_model(
    tmp_path: Path, server: StubServer, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _config(tmp_path, server.base_url)
    code = cli.main(["run", "Settle order ORD-1001", "--config", str(path)])
    assert code == 0
    assert "FIRST_PASS" in capsys.readouterr().out


def test_cli_run_with_a_dead_endpoint_reports_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _config(tmp_path, "http://127.0.0.1:1/v1")
    code = cli.main(["run", "Settle order", "--config", str(path)])
    assert code == 1
    err = capsys.readouterr().err
    assert "internal error" in err or "could not reach" in err
