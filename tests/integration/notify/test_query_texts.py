"""Texts of /pnl, /report and /config (PostgreSQL)."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from tests.support.risk import CONDITIONS, NOW, closed_position
from trade_agent.notify.status import config_hash, config_text, pnl_text, report_text
from trade_agent.persistence.db import Database
from trade_agent.persistence.research_store import ReportEntry, ResearchStore
from trade_agent.persistence.store import Store
from trade_agent.strategy.profiles import load_strategy_config

ROOT = Path(__file__).parents[3]
PROFILES = load_strategy_config(ROOT / "tests" / "fixtures" / "profiles.yaml")


async def test_pnl_by_period_and_profile(store: Store) -> None:
    assert "Invalid period" in await pnl_text(store, PROFILES, ["ano"], NOW)
    await closed_position(store, profile="con", pnl="5", closed_at=NOW - timedelta(hours=2))
    await closed_position(store, profile="mod", pnl="-2", closed_at=NOW - timedelta(hours=3))
    await closed_position(store, profile="xyz", pnl="1", closed_at=NOW - timedelta(days=3))
    await closed_position(store, profile="con", pnl="-9", closed_at=NOW - timedelta(days=20))
    day = await pnl_text(store, PROFILES, [], NOW)
    assert day.splitlines() == [
        "Realized PnL (day): 3 USDT over 2 trades (1 winning)",
        "• conservador: 5",
        "• moderado: -2",
    ]
    week = await pnl_text(store, PROFILES, ["WEEK"], NOW)
    assert week.splitlines()[0] == "Realized PnL (week): 4 USDT over 3 trades (2 winning)"
    assert "• xyz: 1" in week  # a code with no configured profile shows up as it is
    month = await pnl_text(store, PROFILES, ["month"], NOW)
    assert month.splitlines()[0] == "Realized PnL (month): -5 USDT over 4 trades (2 winning)"


async def test_report_text(db: Database) -> None:
    research = ResearchStore(db)
    assert await report_text(research) == "Analyst: no valid reading."
    view = {
        "market_regime": "risk_off", "exposure_multiplier": 0.5, "global_sentiment": -0.4,
        "global_risk_flags": ["FOMC hoje"],
        "assets": [
            {"asset": "SOL", "sentiment": -0.8, "confidence": 0.9, "veto": True,
             "rationale": "exploit confirmado"},
            {"asset": "BTC", "sentiment": 0.1, "confidence": 0.4, "veto": False,
             "rationale": "neutro"},
        ],
    }  # fmt: skip
    await research.add_report(
        ReportEntry(
            as_of=NOW, trigger="t", status="ok", model="claude-opus-5", prompt_version="v1",
            view=view, draft=None, adjustments=[], sources=[], error=None,
            cost_usd=Decimal("0.0312"),
        )
    )  # fmt: skip
    assert (await report_text(research)).splitlines() == [
        "Reading of 2026-09-26 12:00 UTC (claude-opus-5, US$ 0.0312): regime risk_off, "
        "exposure 0.5, sentiment -0.4",
        "⚠️ FOMC hoje",
        "• SOL VETO: -0.80 (confidence 0.90) — exploit confirmado",
        "• BTC: +0.10 (confidence 0.40) — neutro",
    ]


def test_config_text_and_hash(tmp_path: Path) -> None:
    first = tmp_path / "a.yaml"
    first.write_text("x: 1", encoding="utf-8")
    digest = config_hash([first])
    assert len(digest) == 12
    first.write_text("x: 2", encoding="utf-8")
    assert config_hash([first]) != digest
    text = config_text(PROFILES, CONDITIONS, "abc123")
    lines = text.splitlines()
    assert lines[0] == "Configuration abc123 — managed capital 1000"
    assert lines[1].startswith("• conservador (con): 4h, capital 500.0, risk/trade 0.5%, TP 3%")
    assert "quote_depeg_pct" in lines[-1] and "profit_target_pct" not in lines[-1]
