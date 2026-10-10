"""Runtime background tasks and application composition (Phase 5)."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from structlog.testing import capture_logs

from tests.support.candles import raw_klines, uptrend_with_pullback
from tests.support.claude import FakeClaude
from tests.support.fake_binance import FakeBinance
from tests.support.logs import COMPOSE
from trade_agent.app import assemble
from trade_agent.config.settings import Settings, load_settings
from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.execution.service import PositionService
from trade_agent.notify.notifier import LogNotifier, TelegramNotifier
from trade_agent.persistence.db import Database
from trade_agent.persistence.store import Store
from trade_agent.reconcile.reconciler import Reconciler, ReconcileReport
from trade_agent.research.config import SourcesConfig, load_research_config
from trade_agent.research.models import FearGreed, MarketMetrics
from trade_agent.risk.conditions import load_stop_conditions
from trade_agent.risk.state import GLOBAL, OpState
from trade_agent.runtime import AgentRuntime
from trade_agent.strategy.profiles import load_strategy_config

ROOT = Path(__file__).parents[2]
STRATEGY = load_strategy_config(ROOT / "tests" / "fixtures" / "profiles.yaml")


async def test_background_jobs_services_and_reconcile_hook(
    db: Database, api: BinanceSpotApi, store: Store, service: PositionService
) -> None:
    stop = asyncio.Event()
    ticks: list[str] = []
    reports: list[ReconcileReport] = []
    attempts: list[int] = []

    async def periodic() -> None:
        ticks.append("periodic")

    async def hourly() -> None:
        ticks.append("imediato")  # long interval: only runs because it runs at startup

    async def heartbeat() -> None:
        ticks.append("heartbeat")

    async def on_candle() -> None:
        ticks.append("candle")
        raise RuntimeError("falha no ciclo")  # recorded, does not break the scheduling

    async def flaky_service(event: asyncio.Event) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("serviço caiu")
        await event.wait()

    async def on_reconcile(report: ReconcileReport) -> None:
        reports.append(report)
        if len(reports) >= 2 and {"candle", "periodic", "imediato", "heartbeat"} <= set(ticks):
            stop.set()

    runtime = AgentRuntime(
        db=db,
        api=api,
        store=store,
        reconciler=Reconciler(api, service, store),
        reconcile_interval_s=0.02,
        periodic=[(0.01, periodic), (3600, hourly)],
        heartbeat=heartbeat,
        heartbeat_interval_s=3600,
        candle_jobs=[("1m", on_candle)],
        services=[flaky_service],
        on_reconcile=on_reconcile,
        candle_delay=timedelta(milliseconds=10),
        service_backoff_s=0.01,
        clock=lambda: datetime(2026, 9, 26, 16, tzinfo=UTC),  # frozen at a close
    )
    with capture_logs() as logs:
        await asyncio.wait_for(runtime.run(stop), timeout=15)
    assert len(attempts) >= 2  # the service was restarted after the failure
    started = next(e for e in logs if e["event"] == "runtime.tasks_started")
    assert started["candle_jobs"] == ["1m"] and started["services"] == 1
    jobs = {e["job"] for e in logs if e["event"] == "runtime.job"}
    assert any("hourly" in job for job in jobs)
    failed = next(e for e in logs if e["event"] == "runtime.task_failed")
    assert "on_candle" in failed["job"]
    assert ticks.count("imediato") == 1 and ticks.count("heartbeat") == 1
    kinds = [e.kind for e in await store.recent_events(200)]
    assert "runtime.service_failed" in kinds and "runtime.task_failed" in kinds


def test_docker_gives_the_agent_time_to_stop_in_order() -> None:
    """On stop, the agent finishes the task in progress and records agent.stopped. With
    Docker's default 10 s, a news collection (up to timeout_s per source, in parallel)
    would be killed halfway."""
    grace = str(COMPOSE["services"]["agent"]["stop_grace_period"])
    sources = load_research_config(ROOT / "config" / "research.yaml").sources
    assert grace.endswith("s") and int(grace[:-1]) >= 2 * sources.timeout_s


def _settings(**overrides: object) -> Settings:
    return load_settings(env_file=None, **overrides)


async def test_assemble_without_telegram(
    db: Database, api: BinanceSpotApi, store: Store, service: PositionService, fake: FakeBinance
) -> None:
    research = load_research_config(ROOT / "config" / "research.yaml").model_copy(
        update={"sources": SourcesConfig(fear_greed=False, derivatives=False)}
    )
    async with httpx.AsyncClient() as http:
        parts = assemble(
            _settings(),
            api=api, db=db, store=store, positions=service, strategy=STRATEGY,
            conditions=load_stop_conditions(ROOT / "config" / "stop_conditions.yaml"),
            research_config=research, http=http, llm=None,
        )  # fmt: skip
        assert isinstance(parts.notifier, LogNotifier) and parts.commands is None
        assert parts.services == []
        assert [tf for tf, _ in parts.candle_jobs] == ["4h", "1h"]  # enabled profiles
        (risk_s, check_risk), (ingest_s, ingest) = parts.periodic
        assert (risk_s, ingest_s) == (60.0, 900.0)
        assert parts.heartbeat is None
        assert await parts.telemetry.record() is False  # nothing observed yet
        fake.candles[("BTCUSDT", "1m")] = [[0, "0", "0", "0", "100", "1", 0, "1", 1, "0", "0", "0"]]
        await check_risk()  # the first check already records telemetry
        assert (await parts.guard.state(GLOBAL)).state is OpState.RUNNING
        saved = await parts.telemetry.last_recorded()
        assert saved is not None and saved.states["global"] == "running"
        latest = parts.telemetry.latest
        assert latest is not None
        assert await parts.telemetry.observe(latest) is False  # within the interval
        later = replace(latest, now=latest.now + timedelta(minutes=5))
        assert await parts.telemetry.observe(later) is True
        await ingest()
        parts.research.metrics = MarketMetrics(fear_greed=FearGreed(5, "Extreme Fear"))
        await check_risk()  # Fear & Greed below 10 → pause
        assert (await parts.guard.state(GLOBAL)).state is OpState.PAUSED
        await parts.on_reconcile(ReconcileReport(started_at=datetime.now(UTC), orphans=["x"]))
        assert parts.monitor.reconcile_anomalies == 1


@pytest.mark.parametrize(("env", "calls"), [("prod", 1), ("demo", 0), ("testnet", 0)])
async def test_the_delist_schedule_is_asked_only_where_it_exists(
    db: Database,
    api: BinanceSpotApi,
    store: Store,
    service: PositionService,
    fake: FakeBinance,
    env: str,
    calls: int,
) -> None:
    """Testnet and Demo do not have the /sapi routes: no 404 (and warning) every cycle."""
    research = load_research_config(ROOT / "config" / "research.yaml").model_copy(
        update={"sources": SourcesConfig(fear_greed=False, derivatives=False)}
    )
    async with httpx.AsyncClient() as http:
        parts = assemble(
            _settings(binance_env=env),
            api=api, db=db, store=store, positions=service, strategy=STRATEGY,
            conditions=load_stop_conditions(ROOT / "config" / "stop_conditions.yaml"),
            research_config=research, http=http, llm=None,
        )  # fmt: skip
        fake.candles[("BTCUSDT", "4h")] = raw_klines(uptrend_with_pullback())
        with capture_logs() as logs:
            await parts.engine.run_profile("conservador")
    assert len(fake.calls("GET", "/sapi/v1/spot/delist-schedule")) == calls
    assert "universe.delist_schedule_unavailable" not in {log["event"] for log in logs}


async def test_assemble_with_telegram_and_decision_job(
    db: Database,
    api: BinanceSpotApi,
    store: Store,
    service: PositionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        telegram_bot_token="TOKEN",
        telegram_chat_id=7,
        trading_enabled=True,
        healthcheck_url="https://hc.example/ping/abc",
    )
    async with httpx.AsyncClient() as http:
        parts = assemble(
            settings,
            api=api, db=db, store=store, positions=service, strategy=STRATEGY,
            conditions=load_stop_conditions(ROOT / "config" / "stop_conditions.yaml"),
            research_config=load_research_config(ROOT / "config" / "research.yaml"),
            http=http, llm=FakeClaude().client(),
        )  # fmt: skip
        assert isinstance(parts.notifier, TelegramNotifier)
        assert parts.commands is not None and parts.services == [parts.commands.run]
        assert parts.heartbeat is not None
        status = await parts.commands.handle("/status")
        assert status.startswith("Orders: enabled")
        assert (await parts.commands.handle("/positions")) == "Active positions: 0"
        assert (await parts.commands.handle("/pnl week")).startswith("Realized PnL (week)")
        assert (await parts.commands.handle("/report")) == "Analyst: no valid reading."
        assert (await parts.commands.handle("/config")).startswith("Configuration ")

        async def fake_cycle(name: str) -> str:
            return f"ciclo {name}"

        monkeypatch.setattr(parts.engine, "run_profile", fake_cycle)
        assert await parts.candle_jobs[0][1]() == "ciclo conservador"
