import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from lab.paper import CANDIDATES, PaperPortfolio, replay

from tests.unit.strategy.test_portfolio_and_exits import CONFIG, _candidate
from trade_agent.risk.conditions import PreTradeConfig, StopConditions
from trade_agent.strategy.portfolio import MarketOpinion

D = Decimal
NOW = datetime(2026, 10, 20, tzinfo=UTC)


def account(pure: bool, **risk: object) -> PaperPortfolio:
    return PaperPortfolio(
        CONFIG,
        StopConditions(pre_trade=PreTradeConfig(min_reward_risk=1, **risk)),
        pure_ta=pure,
        fee_pct=D(".1"),
        slippage_bps=D(0),
    )


def test_independent_accounts_veto_and_gap_costs() -> None:
    ta, llm = account(True), account(False)
    candidate = _candidate(
        "SOLUSDT", opinion=MarketOpinion(sentiment=D(1), confidence=D(1), veto=True)
    )
    ta.decision("conservador", [candidate], D(".5"), NOW)
    llm.decision("conservador", [candidate], D(".5"), NOW)
    assert "SOLUSDT" in ta.positions and not llm.positions
    position = ta.positions["SOLUSDT"]
    ta.quote("SOLUSDT", D(95), D(96), NOW + timedelta(minutes=1))
    assert not ta.positions and ta.trades[0]["reason"] == "stop"
    assert D(ta.trades[0]["pnl"]) == position.quantity * D(95) * D(".999") - position.spent
    assert llm.summary()["pnl"] == 0


def test_aggregate_limit_and_trailing_exit() -> None:
    limited = account(True, max_portfolio_exposure_pct=0.01)
    limited.decision("conservador", [_candidate("SOLUSDT")], D(1), NOW)
    assert not limited.positions and limited.rejections
    portfolio = account(True)
    portfolio.decision("conservador", [_candidate("SOLUSDT")], D(1), NOW)
    portfolio.quote("SOLUSDT", D(104), D(104), NOW + timedelta(minutes=1))
    portfolio.quote("SOLUSDT", D(102), D(102), NOW + timedelta(minutes=2))
    assert portfolio.trades[0]["reason"] == "take_profit"


def test_replay_schema_and_chronology(tmp_path: Path) -> None:
    tape = tmp_path / "tape.jsonl"
    event = {
        "kind": "decision",
        "at": NOW.isoformat(),
        "profile": "conservador",
        "candidates": CANDIDATES.dump_python([_candidate("SOLUSDT")], mode="json"),
        "exposure_multiplier": "1",
    }
    tape.write_text(json.dumps(event), encoding="utf-8")
    assert replay(tape, {"ta": account(True)})["ta"]["open_positions"] == 1
    event["at"] = (NOW - timedelta(minutes=1)).isoformat()
    tape.write_text(tape.read_text() + "\n" + json.dumps(event), encoding="utf-8")
    with pytest.raises(ValueError, match="chronological"):
        replay(tape, {"ta": account(True)})
