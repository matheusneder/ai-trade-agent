"""Raiz de composição e comando ``trade-agent run`` (PostgreSQL + Binance simulada)."""

import asyncio
import io
from pathlib import Path

import httpx
import pytest

from tests.support.fake_binance import FakeBinance
from trade_agent import cli
from trade_agent.app import build_runtime, install_signal_handlers, run_agent
from trade_agent.config.settings import Settings, load_settings
from trade_agent.exchange.errors import BinanceConfigurationError
from trade_agent.persistence.db import AlreadyRunningError, Database
from trade_agent.persistence.store import Store


def _settings(postgres_url: str | None, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "binance_api_key": "fake-key",
        "binance_key_type": "hmac",
        "binance_api_secret": "fake-secret",
        "database_url": postgres_url,
    }
    values.update(overrides)
    return load_settings(env_file=None, **values)


async def test_build_runtime_requires_database_url() -> None:
    with pytest.raises(BinanceConfigurationError, match="TA_DATABASE_URL"):
        async with build_runtime(_settings(None)):
            pass


async def test_build_runtime_requires_credentials(postgres_url: str) -> None:
    settings = load_settings(env_file=None, database_url=postgres_url)
    with pytest.raises(BinanceConfigurationError, match="credenciais"):
        async with build_runtime(settings):
            pass


async def test_build_runtime_wires_user_stream(postgres_url: str) -> None:
    async with build_runtime(_settings(postgres_url)) as runtime:
        assert runtime._events is not None
    async with build_runtime(_settings(postgres_url), user_stream=False) as runtime:
        assert runtime._events is None


async def test_build_runtime_with_llm_key_and_injected_http(postgres_url: str) -> None:
    settings = _settings(postgres_url, anthropic_api_key="sk-test")
    async with (
        httpx.AsyncClient() as aux,
        build_runtime(settings, aux_http=aux, user_stream=False) as runtime,
    ):
        assert [tf for tf, _ in runtime._candle_jobs] == ["4h"]  # só o conservador
        assert len(runtime._periodic) == 2  # risco e coleta de notícias


async def test_run_agent_starts_recovers_and_stops(postgres_url: str, db: Database) -> None:
    fake = FakeBinance()
    stop = asyncio.Event()
    async with httpx.AsyncClient(base_url="https://fake.binance", transport=fake.transport) as http:
        task = asyncio.create_task(
            run_agent(_settings(postgres_url), stop=stop, http_client=http, user_stream=False)
        )
        store = Store(db)
        for _ in range(100):
            if any(e.kind == "agent.started" for e in await store.recent_events()):
                break
            await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=10)
    kinds = [e.kind for e in await store.recent_events()]
    assert kinds[:2] == ["agent.stopped", "agent.started"]
    assert fake.calls("GET", "/api/v3/time")


async def test_install_signal_handlers_is_safe() -> None:
    install_signal_handlers(asyncio.Event())  # no Windows, o SO não suporta: ignorado


def _run_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception | None
) -> tuple[int, str]:
    calls: list[Settings] = []

    async def fake_run_agent(settings: Settings) -> None:
        calls.append(settings)
        if error is not None:
            raise error

    monkeypatch.setattr(cli, "run_agent", fake_run_agent)
    env_file = tmp_path / ".env"
    env_file.write_text("TA_LOG_LEVEL=WARNING\n", encoding="utf-8")
    err = io.StringIO()
    code = cli.main(["--env-file", str(env_file), "run"], out=io.StringIO(), err=err)
    assert len(calls) == 1
    return code, err.getvalue()


@pytest.mark.parametrize(
    ("error", "code", "message"),
    [
        (None, 0, ""),
        (AlreadyRunningError("outra instância"), 1, "outra instância"),
        (BinanceConfigurationError("sem banco"), 1, "sem banco"),
    ],
)
def test_cli_run_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: Exception | None,
    code: int,
    message: str,
) -> None:
    result, err = _run_cli(monkeypatch, tmp_path, error)
    assert result == code
    assert message in err
