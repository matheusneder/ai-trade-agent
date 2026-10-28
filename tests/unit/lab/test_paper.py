import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from lab.paper import CANDIDATES, PaperPortfolio, replay

from tests.support.local_profiles import local_config
from tests.unit.strategy.test_portfolio_and_exits import CONFIG, _candidate
from trade_agent.risk.conditions import PreTradeConfig, StopConditions
from trade_agent.strategy.portfolio import Candidate, MarketOpinion

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


def _local_account(**overrides: object) -> PaperPortfolio:
    config = local_config(local={"timeframe": "4h", **overrides})
    return PaperPortfolio(
        config,
        StopConditions(pre_trade=PreTradeConfig(min_reward_risk=1)),
        pure_ta=True,
        fee_pct=D(".1"),
        slippage_bps=D(10),
        profiles=["local"],
    )


def _local_candidate(symbol: str = "SOLUSDT", **signal: Any) -> Candidate:
    candidate = _candidate(symbol, setup="pair_pullback", stop_pct=0.03)
    return replace(candidate, signal=replace(candidate.signal, **signal))


def test_a_local_limit_target_fills_at_its_price() -> None:
    account = _local_account()
    account.decision("local", [_local_candidate()], D(1), NOW)
    position = account.positions["SOLUSDT"]
    assert position.target is not None and position.target > position.entry
    account.quote("SOLUSDT", position.target - D("0.01"), position.target, NOW + timedelta(hours=1))
    assert account.positions  # just below: the limit order waits
    account.quote(
        "SOLUSDT",
        position.target * D("1.02"),
        position.target * D("1.03"),
        NOW + timedelta(hours=2),
    )
    (trade,) = account.trades
    assert trade["reason"] == "target"
    assert D(trade["pnl"]) == position.quantity * position.target * D(".999") - position.spent


def test_a_local_technical_exit_and_the_breakout_level() -> None:
    account = _local_account()
    account.decision("local", [_local_candidate()], D(1), NOW)
    exit_now = _local_candidate(setup=None, exit_signal=True)
    account.decision("local", [exit_now], D(1), NOW + timedelta(hours=4))
    assert account.trades[0]["reason"] == "decision" and not account.positions
    breakout = _local_account(
        family="pair_volatility_breakout",
        protection={"take_profit": {"mode": "trailing", "activation_r": 3, "trailing_atr_mult": 1}},
    )
    breakout.decision("local", [_local_candidate(setup="pair_breakout", level=99.0)], D(1), NOW)
    assert breakout.positions["SOLUSDT"].level == 99.0
    held = _local_candidate(setup=None, close=99.5)
    breakout.decision("local", [held], D(1), NOW + timedelta(hours=4))
    assert breakout.positions  # still above the level
    below = replace(held, bid=D(98), ask=D(98), signal=replace(held.signal, close=98.5))
    breakout.decision("local", [below], D(1), NOW + timedelta(hours=8))
    assert not breakout.positions
    expired = _local_account()
    expired.decision("local", [_local_candidate()], D(1), NOW)
    expired.decision("local", [_local_candidate(setup=None)], D(1), NOW + timedelta(hours=48))
    assert expired.trades[0]["reason"] == "decision"


def test_the_scope_pauses_a_local_profile_after_its_stops() -> None:
    account = _local_account(risk={"daily_loss_pct": None})
    stops = 0
    for k, symbol in enumerate(("SOLUSDT", "ETHUSDT", "ADAUSDT")):
        at = NOW + timedelta(hours=k)
        account.decision("local", [_local_candidate(symbol)], D(1), at)
        account.quote(symbol, D("96.9"), D("96.9"), at + timedelta(minutes=30))  # the stop
        stops += 1
    assert [t["reason"] for t in account.trades] == ["stop"] * stops
    account.decision("local", [_local_candidate("XRPUSDT")], D(1), NOW + timedelta(hours=4))
    assert not account.positions
    assert account.rejections[-1] == {"symbol": "XRPUSDT", "reason": "perdas seguidas do perfil"}
    # an account without the profile ignores its decisions
    other = _local_account()
    other.decision("conservador", [_candidate("SOLUSDT")], D(1), NOW)
    assert not other.positions


def test_paper_refuses_a_trailing_stop() -> None:
    with pytest.raises(ValueError, match="fixed stops only"):
        PaperPortfolio(
            CONFIG,
            StopConditions(),
            pure_ta=True,
            fee_pct=D(0),
            slippage_bps=D(0),
            profiles=["agressivo"],
        )
