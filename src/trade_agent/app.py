"""Raiz de composição: monta os componentes do agente a partir das configurações."""

import asyncio
import contextlib
import signal
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import anthropic
import httpx
import structlog

from trade_agent.config.settings import Settings
from trade_agent.decision.engine import DecisionEngine, UniverseCache
from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.exchange.environments import endpoints_for
from trade_agent.exchange.errors import BinanceConfigurationError
from trade_agent.exchange.factory import build_rest_client, build_signer
from trade_agent.exchange.user_stream import UserDataStream
from trade_agent.execution.gateway import ExecutionGateway
from trade_agent.execution.service import PositionService, RulesCache
from trade_agent.market.universe import UniverseConfig
from trade_agent.metrics import configure_metrics
from trade_agent.metrics import install as install_metrics
from trade_agent.notify.commands import CommandCenter, Query
from trade_agent.notify.notifier import LogNotifier, Notifier, TelegramNotifier
from trade_agent.notify.status import (
    config_hash,
    config_text,
    pnl_text,
    positions_text,
    report_text,
    status_text,
)
from trade_agent.notify.telegram import TelegramBot
from trade_agent.persistence.db import Database
from trade_agent.persistence.research_store import ResearchStore
from trade_agent.persistence.store import Severity, Store
from trade_agent.reconcile.reconciler import Reconciler, ReconcileReport
from trade_agent.research.collector import NewsCollector
from trade_agent.research.config import ResearchConfig, load_research_config
from trade_agent.research.service import ResearchService
from trade_agent.research.tagging import AssetTagger
from trade_agent.risk.conditions import StopConditions, load_stop_conditions
from trade_agent.risk.guard import RiskGuard, evaluate
from trade_agent.risk.monitor import RiskMonitor
from trade_agent.risk.state import StateStore
from trade_agent.runtime import Action, AgentRuntime, Service
from trade_agent.strategy.profiles import StrategyConfig, load_strategy_config
from trade_agent.telemetry.heartbeat import Heartbeat
from trade_agent.telemetry.recorder import TelemetryRecorder
from trade_agent.tracing import configure_tracing
from trade_agent.tracing import install as install_tracing

RISK_INTERVAL_S = 60.0
INGEST_INTERVAL_S = 900.0

log = structlog.get_logger(__name__)


@dataclass
class AgentParts:
    guard: RiskGuard
    engine: DecisionEngine
    monitor: RiskMonitor
    research: ResearchService
    notifier: Notifier
    telemetry: TelemetryRecorder
    commands: CommandCenter | None
    heartbeat: Heartbeat | None = None
    periodic: list[tuple[float, Action]] = field(default_factory=list)
    candle_jobs: list[tuple[str, Action]] = field(default_factory=list)
    services: list[Service] = field(default_factory=list)

    async def on_reconcile(self, report: ReconcileReport) -> None:
        self.monitor.note_reconcile(report)


def assemble(
    settings: Settings,
    *,
    api: BinanceSpotApi,
    db: Database,
    store: Store,
    positions: PositionService,
    strategy: StrategyConfig,
    conditions: StopConditions,
    research_config: ResearchConfig,
    http: httpx.AsyncClient,
    llm: anthropic.AsyncAnthropic | None,
) -> AgentParts:
    """Liga risco, analista, motor de decisão e Telegram (sem abrir conexões)."""
    notifier: Notifier = LogNotifier()
    bot: TelegramBot | None = None
    if settings.telegram_bot_token is not None and settings.telegram_chat_id is not None:
        bot = TelegramBot(http, settings.telegram_bot_token.get_secret_value())
        notifier = TelegramNotifier(bot, settings.telegram_chat_id, min_severity=Severity.INFO)

    guard = RiskGuard(
        conditions=conditions, states=StateStore(store), store=store, notify=notifier.notify
    )
    research_store = ResearchStore(db)
    names = research_config.asset_names
    research = ResearchService(
        store=research_store,
        collector=NewsCollector(http, research_config.sources, AssetTagger(names, names)),
        client=llm,
        config=research_config,
    )
    engine = DecisionEngine(
        api=api,
        positions=positions,
        store=store,
        guard=guard,
        strategy=strategy,
        universe=UniverseCache(
            api,
            UniverseConfig(
                quote_asset=strategy.account.quote_asset,
                delist_schedule=endpoints_for(settings.binance_env).sapi,
            ),
        ),
        research=research,
        view_max_age=timedelta(hours=research_config.safety.max_view_age_hours),
        dry_run=not settings.trading_enabled,
        notify=notifier.notify,
    )
    guard.set_flattener(engine.flatten)

    def fear_greed() -> int | None:
        reading = research.metrics.fear_greed
        return reading.value if reading else None

    monitor = RiskMonitor(
        api=api, store=store, strategy=strategy, health=api.rest.health, fear_greed=fear_greed
    )

    telemetry = TelemetryRecorder(db=db, guard=guard, rest=api.rest, scopes=list(strategy.profiles))

    async def check_risk() -> None:
        snapshot = await monitor.snapshot()
        await telemetry.observe(snapshot)
        await guard.apply(evaluate(conditions, snapshot))

    symbols = [f"{asset}{strategy.account.quote_asset}" for asset in names]

    async def ingest() -> None:
        await research.ingest(derivative_symbols=symbols)

    def decide(name: str) -> Action:
        async def action() -> object:
            return await engine.run_profile(name)

        action.__qualname__ = f"decide_{name}"  # nome da tarefa nos logs e traces
        return action

    commands: CommandCenter | None = None
    services: list[Service] = []
    if bot is not None and settings.telegram_chat_id is not None:
        digest = config_hash(
            [settings.strategy_config, settings.stop_conditions, settings.research_config]
        )

        async def status_query(_: list[str]) -> str:
            return await status_text(
                guard=guard,
                store=store,
                research=research_store,
                strategy=strategy,
                trading_enabled=settings.trading_enabled,
            )

        async def positions_query(_: list[str]) -> str:
            return await positions_text(store)

        async def pnl_query(args: list[str]) -> str:
            return await pnl_text(store, strategy, args, datetime.now(UTC))

        async def report_query(_: list[str]) -> str:
            return await report_text(research_store)

        async def config_query(_: list[str]) -> str:
            return config_text(strategy, conditions, digest)

        queries: dict[str, Query] = {
            "/status": status_query,
            "/positions": positions_query,
            "/pnl": pnl_query,
            "/report": report_query,
            "/config": config_query,
        }
        commands = CommandCenter(
            bot=bot,
            chat_id=settings.telegram_chat_id,
            guard=guard,
            store=store,
            scopes=list(strategy.profiles),
            queries=queries,
        )
        services.append(commands.run)

    heartbeat = (
        Heartbeat(http, settings.healthcheck_url.get_secret_value())
        if settings.healthcheck_url is not None
        else None
    )
    log.debug(
        "app.assembled",
        env=settings.binance_env.value,
        dry_run=not settings.trading_enabled,
        profiles=list(strategy.enabled_profiles()),
        telegram=commands is not None,
        heartbeat=heartbeat is not None,
        llm=llm is not None,
        llm_budget_usd=str(research_config.budget.daily_usd),
        tracing=settings.otlp_endpoint is not None,
        metrics=settings.otlp_metrics_endpoint is not None,
    )
    return AgentParts(
        guard=guard,
        engine=engine,
        monitor=monitor,
        research=research,
        notifier=notifier,
        telemetry=telemetry,
        commands=commands,
        heartbeat=heartbeat,
        periodic=[(RISK_INTERVAL_S, check_risk), (INGEST_INTERVAL_S, ingest)],
        candle_jobs=[
            (profile.timeframe, decide(name))
            for name, profile in strategy.enabled_profiles().items()
        ],
        services=services,
    )


@asynccontextmanager
async def build_runtime(
    settings: Settings,
    *,
    http_client: httpx.AsyncClient | None = None,
    aux_http: httpx.AsyncClient | None = None,
    llm: anthropic.AsyncAnthropic | None = None,
    user_stream: bool = True,
) -> AsyncIterator[AgentRuntime]:
    """Cria o runtime completo; libera conexões (HTTP e banco) ao sair."""
    if settings.database_url is None:
        raise BinanceConfigurationError("defina TA_DATABASE_URL para executar o agente")
    signer = build_signer(settings)
    if signer is None or settings.binance_api_key is None:
        raise BinanceConfigurationError("o agente exige credenciais da Binance no .env")
    strategy = load_strategy_config(settings.strategy_config)
    conditions = load_stop_conditions(settings.stop_conditions)
    research_config = load_research_config(settings.research_config)
    if llm is None and settings.anthropic_api_key is not None:
        llm = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
    db = Database(settings.database_url.get_secret_value())
    try:
        async with AsyncExitStack() as stack:
            rest = await stack.enter_async_context(
                build_rest_client(settings, http_client=http_client)
            )
            if aux_http is None:
                aux_http = await stack.enter_async_context(
                    httpx.AsyncClient(timeout=research_config.sources.timeout_s)
                )
            api = BinanceSpotApi(rest)
            store = Store(db)
            positions = PositionService(api, ExecutionGateway(api), store, RulesCache(api))
            parts = assemble(
                settings,
                api=api,
                db=db,
                store=store,
                positions=positions,
                strategy=strategy,
                conditions=conditions,
                research_config=research_config,
                http=aux_http,
                llm=llm,
            )
            stream = UserDataStream(
                endpoints_for(settings.binance_env).ws_api,
                settings.binance_api_key.get_secret_value(),
                signer,
                now_ms=rest.now_ms,
                recv_window_ms=settings.binance_recv_window_ms,
            )
            yield AgentRuntime(
                db=db,
                api=api,
                store=store,
                reconciler=Reconciler(api, positions, store),
                events=stream.events if user_stream else None,
                heartbeat=parts.heartbeat.ping if parts.heartbeat else None,
                periodic=parts.periodic,
                candle_jobs=parts.candle_jobs,
                services=parts.services,
                on_reconcile=parts.on_reconcile,
            )
    finally:
        await db.dispose()


def install_signal_handlers(stop: asyncio.Event) -> None:
    """SIGTERM/SIGINT encerram o agente de forma ordenada (onde o SO permite)."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)


async def run_agent(
    settings: Settings,
    *,
    stop: asyncio.Event | None = None,
    http_client: httpx.AsyncClient | None = None,
    aux_http: httpx.AsyncClient | None = None,
    llm: anthropic.AsyncAnthropic | None = None,
    user_stream: bool = True,
) -> None:
    stop = stop or asyncio.Event()
    install_signal_handlers(stop)
    environment = settings.binance_env.value
    traces = configure_tracing(settings.otlp_endpoint, environment=environment)
    agent_metrics = configure_metrics(settings.otlp_metrics_endpoint, environment=environment)
    try:
        async with build_runtime(
            settings, http_client=http_client, aux_http=aux_http, llm=llm, user_stream=user_stream
        ) as runtime:
            await runtime.run(stop)
    finally:
        if traces is not None:
            traces.shutdown()  # envia os spans que ainda estão na fila
        if agent_metrics is not None:
            agent_metrics.shutdown()  # última exportação das métricas
        install_tracing(None)
        install_metrics(None)
