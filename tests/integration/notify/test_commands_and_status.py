"""Comandos do Telegram e /status (PostgreSQL + Bot API simulada)."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from tests.support.risk import CONDITIONS, NOW, POLICY, snapshot
from trade_agent.execution.orders import EntryOrder
from trade_agent.execution.service import PositionService
from trade_agent.notify.commands import OFFSET_KEY, CommandCenter
from trade_agent.notify.status import status_text
from trade_agent.notify.telegram import TelegramBot, TelegramError
from trade_agent.persistence.db import Database
from trade_agent.persistence.research_store import ReportEntry, ResearchStore
from trade_agent.persistence.store import Severity, Store
from trade_agent.risk.guard import RiskGuard, evaluate
from trade_agent.risk.state import GLOBAL, OpState, ScopeState, StateStore
from trade_agent.strategy.profiles import load_strategy_config

D = Decimal
API = "https://tg.example/botTOKEN"
CHAT = 4242
PROFILES = load_strategy_config(Path(__file__).parents[2] / "fixtures" / "profiles.yaml")


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


def _center(store: Store, http: httpx.AsyncClient, clock: Clock) -> tuple[CommandCenter, RiskGuard]:
    async def notify(severity: Severity, text: str) -> None:
        return None

    guard = RiskGuard(
        conditions=CONDITIONS, states=StateStore(store), store=store, notify=notify, clock=clock
    )
    flattened: list[str] = []

    async def flatten(scope: str) -> int:
        flattened.append(scope)
        return 1

    guard.set_flattener(flatten)

    async def status(args: list[str]) -> str:
        return f"tudo certo {args}"

    center = CommandCenter(
        bot=TelegramBot(http, "TOKEN", base_url="https://tg.example"),
        chat_id=CHAT,
        guard=guard,
        store=store,
        scopes=["conservador", "moderado"],
        queries={"/status": status},
        clock=clock,
        new_code=lambda: "ABC123",
    )
    return center, guard


async def test_commands_change_operational_states(store: Store, http: httpx.AsyncClient) -> None:
    clock = Clock()
    center, guard = _center(store, http, clock)
    assert (await center.handle("oi")).startswith("Consultas: /status. Controle:")
    assert (await center.handle("/desconhecido")).startswith("Consultas:")
    assert await center.handle("/status@meubot x") == "tudo certo ['x']"
    assert "Escopo inválido" in await center.handle("/pause marte")
    assert "pausado" in await center.handle("/pause conservador")
    assert (await guard.state("conservador")).state is OpState.PAUSED
    assert "parado" in await center.handle("/halt")
    assert (await guard.state(GLOBAL)).state is OpState.HALTED
    assert await center.handle("/resume") == "global: retomado."
    assert await center.handle("/resume conservador") == "conservador: retomado."
    assert (await guard.effective("conservador")) is OpState.RUNNING
    await guard.apply(evaluate(CONDITIONS, snapshot(consecutive_losses=4)))
    assert await center.handle("/resume") == (
        "global: retomado. Ainda valendo, só voltam a disparar se piorarem mais um limite: "
        "max_consecutive_losses: 4 (limite 4)."
    )
    assert await center.handle("/resume conservador") == "conservador: retomado."

    await StateStore(store).put("moderado", ScopeState(OpState.FLATTENING))
    assert "aguarde" in await center.handle("/resume moderado")


async def test_flatten_requires_confirmation_code(store: Store, http: httpx.AsyncClient) -> None:
    clock = Clock()
    center, guard = _center(store, http, clock)
    prompt = await center.handle("/flatten moderado")
    assert "/flatten moderado ABC123" in prompt and "VENDE" in prompt
    assert "incorreto" in await center.handle("/flatten moderado XXXXXX")
    assert "Confirme" in await center.handle("/flatten moderado")  # novo código
    clock.now += timedelta(minutes=3)
    assert "Confirme" in await center.handle("/flatten moderado ABC123")  # expirado: pede de novo
    confirmed = await center.handle("/flatten moderado abc123")
    assert confirmed == "moderado: flatten concluído (1 posições). Estado: halted."
    assert (await guard.state("moderado")).state is OpState.HALTED
    assert "Confirme" in await center.handle("/flatten moderado ABC123")  # código já usado


async def test_polling_authorization_and_offset(store: Store, http: httpx.AsyncClient) -> None:
    center, guard = _center(store, http, Clock())
    with respx.mock() as router:
        updates = router.post(f"{API}/getUpdates").mock(
            side_effect=[
                httpx.Response(200, json={"ok": True, "result": [
                    {"update_id": 10, "message": {"chat": {"id": 999}, "text": "/halt"}},
                    {"update_id": 11, "message": {"chat": {"id": CHAT}, "text": "/pause"}},
                ]}),
                httpx.Response(200, json={"ok": True, "result": []}),
            ]
        )  # fmt: skip
        send = router.post(f"{API}/sendMessage").respond(200, json={"ok": True, "result": {}})
        assert await center.poll_once(timeout_s=1) == 2
        assert await center.poll_once(timeout_s=1) == 0
    assert (await guard.state(GLOBAL)).state is OpState.PAUSED  # o /halt de outro chat foi ignorado
    assert await store.get_checkpoint(OFFSET_KEY) == {"offset": 12}
    assert json.loads(updates.calls[1].request.content)["offset"] == 12
    assert len(send.calls) == 1
    kinds = [e.kind for e in await store.recent_events()]
    assert "telegram.unauthorized" in kinds and "telegram.command" in kinds


async def test_run_loop_backs_off_on_errors_and_stops(
    store: Store, http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    center, _ = _center(store, http, Clock())
    stop = asyncio.Event()
    calls: list[int] = []

    async def failing(*, timeout_s: int = 30) -> int:
        calls.append(timeout_s)
        if len(calls) == 2:
            stop.set()
        raise TelegramError("fora do ar")

    monkeypatch.setattr(center, "poll_once", failing)
    await asyncio.wait_for(center.run(stop, backoff_s=0.01, timeout_s=1), timeout=5)
    assert calls == [1, 1]


async def test_status_text(db: Database, store: Store, service: PositionService) -> None:
    async def notify(severity: Severity, text: str) -> None:
        return None

    guard = RiskGuard(
        conditions=CONDITIONS, states=StateStore(store), store=store, notify=notify,
        clock=lambda: NOW,
    )  # fmt: skip
    research = ResearchStore(db)
    empty = await status_text(
        guard=guard, store=store, research=research, strategy=PROFILES, trading_enabled=False
    )
    assert empty.splitlines() == [
        "Ordens: SIMULAÇÃO (trava desligada)",
        "global: running",
        "conservador: running",
        "moderado: running",
        "agressivo: running [desabilitado]",
        "Posições ativas: 0",
        "Analista: sem leitura válida",
    ]
    await guard.pause("moderado", "manual")
    await StateStore(store).put(
        GLOBAL,
        ScopeState(OpState.PAUSED, "btc", NOW, NOW + timedelta(hours=4)),
    )
    await service.open_position(
        profile="con", entry=EntryOrder("BTCUSDT", D("0.001"), D(63100)), policy=POLICY
    )
    await research.add_report(
        ReportEntry(
            as_of=NOW, trigger="t", status="ok", model="m", prompt_version="v1",
            view={"market_regime": "risk_off", "exposure_multiplier": 0.5}, draft=None,
            adjustments=[], sources=[], error=None, cost_usd=D(0),
        )
    )  # fmt: skip
    text = await status_text(
        guard=guard, store=store, research=research, strategy=PROFILES, trading_enabled=True
    )
    lines = text.splitlines()
    assert lines[0] == "Ordens: habilitadas"
    assert lines[1] == "global: paused (até 26/09 16:00 UTC) — btc"
    assert lines[3] == "moderado: paused — manual"
    assert lines[6] == "• BTCUSDT [con] protected entrada 63000 qtd 0.00099"  # líquida da taxa
    assert lines[7] == "Analista (26/09 12:00 UTC): regime risk_off, exposição 0.5"
