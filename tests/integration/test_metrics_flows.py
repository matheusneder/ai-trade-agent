"""Agent metrics in the real flows (PostgreSQL, simulated Binance, fake Claude)."""

from decimal import Decimal
from pathlib import Path
from typing import Any

from tests.support.claude import FakeClaude
from tests.support.fake_binance import FakeBinance
from tests.support.metrics import Measured
from tests.support.risk import CONDITIONS
from trade_agent.exchange.api import BinanceSpotApi
from trade_agent.persistence.db import Database
from trade_agent.persistence.store import Severity, Store
from trade_agent.research.models import TriageDraft
from trade_agent.risk.guard import RiskGuard
from trade_agent.risk.monitor import RiskMonitor
from trade_agent.risk.state import StateStore
from trade_agent.strategy.profiles import load_strategy_config
from trade_agent.telemetry.recorder import TelemetryRecorder

PROFILES = load_strategy_config(Path(__file__).parents[1] / "fixtures" / "profiles.yaml")


def _klines(closes: list[float]) -> list[list[Any]]:
    return [[i * 60_000, "0", "0", "0", str(c), "1", i * 60_000 + 59_999, "1", 1, "0", "0", "0"]
            for i, c in enumerate(closes)]  # fmt: skip


async def _ignore(_severity: Severity, _text: str) -> None:
    return None


async def test_risk_check_feeds_the_gauges(
    measured: Measured, db: Database, api: BinanceSpotApi, store: Store, fake: FakeBinance
) -> None:
    fake.candles[("BTCUSDT", "1m")] = _klines([60000.0] * 61)
    guard = RiskGuard(conditions=CONDITIONS, states=StateStore(store), store=store, notify=_ignore)
    await guard.pause("conservador", "teste")  # counts one state change
    monitor = RiskMonitor(api=api, store=store, strategy=PROFILES, health=api.rest.health)
    recorder = TelemetryRecorder(db=db, guard=guard, rest=api.rest, scopes=list(PROFILES.profiles))
    await api.rest.sync_time()
    await recorder.observe(await monitor.snapshot())
    assert measured.value("trade_agent.equity") == 1000  # managed capital, no positions
    assert measured.value("trade_agent.positions.active") == 0
    assert measured.value("trade_agent.risk.state", scope="global") == 0
    assert measured.value("trade_agent.risk.state", scope="conservador") == 1  # paused
    assert measured.value("trade_agent.clock.offset") == api.rest.time_offset_ms
    assert (
        measured.value(
            "trade_agent.risk.state_changes", scope="conservador", state="paused", source="manual"
        )
        == 1
    )


async def test_llm_calls_count_cost_and_tokens(measured: Measured) -> None:
    fake = FakeClaude()
    fake.reply_json(TriageDraft(items=[]), usage={"input_tokens": 100, "output_tokens": 10})
    await fake.claude().structured(
        purpose="triage", model="claude-sonnet-5", system="S", content="C",
        output=TriageDraft, max_tokens=2000,
    )  # fmt: skip
    llm = {"model": "claude-sonnet-5", "purpose": "triage"}
    assert Decimal(str(measured.value("trade_agent.llm.cost", **llm))) > 0
    assert measured.value("trade_agent.llm.tokens", **llm, direction="input") == 100
    assert measured.value("trade_agent.llm.tokens", **llm, direction="output") == 10
