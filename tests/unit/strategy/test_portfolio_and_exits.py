from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support.exchange_info import rules_for
from trade_agent.execution.orders import (
    EntryMode,
    FixedStop,
    ProtectionPolicy,
    StopMode,
    TakeProfitMode,
    TrailingTakeProfit,
)
from trade_agent.execution.positions import Position, PositionState
from trade_agent.market.universe import Tier, UniverseMember
from trade_agent.signals import Signal
from trade_agent.strategy.exits import break_even_protection, exit_reason, next_weak_cycles
from trade_agent.strategy.portfolio import Candidate, Holding, MarketOpinion, plan_entries
from trade_agent.strategy.profiles import ProfileConfig, load_strategy_config

D = Decimal
CONFIG = load_strategy_config(Path(__file__).parents[2] / "fixtures" / "profiles.yaml")
CONSERVATIVE = CONFIG.profiles["conservador"]
MODERATE = CONFIG.profiles["moderado"]


def _member(symbol: str, tier: Tier) -> UniverseMember:
    rules = replace(rules_for("SOLUSDT"), symbol=symbol)
    return UniverseMember(symbol, symbol[:-4], tier, 1, D("1e8"), D("1"), rules)


def _candidate(
    symbol: str,
    tier: Tier = Tier.CORE,
    *,
    score: float = 0.8,
    setup: str | None = "trend_pullback",
    stop_pct: float = 0.02,
    price: str = "100",
    opinion: MarketOpinion | None = None,
) -> Candidate:
    signal = Signal(score=score, setup=setup, stop_pct=stop_pct, atr_pct=0.01, close=float(price))
    return Candidate(_member(symbol, tier), signal, bid=D(price), ask=D(price), opinion=opinion)


def _plan(profile: ProfileConfig = CONSERVATIVE, **kw: object):  # type: ignore[no-untyped-def]
    args: dict[str, object] = {
        "name": "conservador",
        "profile": profile,
        "capital": D("1000"),
        "holdings": [],
        "candidates": [],
    }
    args.update(kw)
    return plan_entries(**args)  # type: ignore[arg-type]


def test_sizing_by_risk_per_trade() -> None:
    result = _plan(candidates=[_candidate("AAAUSDT")])
    [idea] = result.ideas
    # risco 0,5% de 1000 = 5; stop 2% → notional 250; limite por posição 25% = 250
    assert idea.entry.quantity == D("2.496")  # 250 / 100,15 = 2,4962…, arredondado ao step
    assert idea.entry.limit_price == D("100.15")
    assert idea.entry.mode is EntryMode.LIMIT_FOK
    assert idea.policy.stop_pct == D("2")
    assert idea.risk == pytest.approx(idea.notional * D("0.02"))
    assert idea.profile_code == "con"
    assert result.rejections == ()


def test_ranking_limits_and_rejections() -> None:
    holdings = [
        Holding("conservador", "HELDUSDT", Tier.CORE, D("100")),
        Holding("moderado", "OTHERUSDT", Tier.CORE, D("50")),
    ]
    candidates = [
        _candidate("LOWUSDT", score=0.3),
        _candidate("NOSETUSDT", setup=None),
        _candidate("HELDUSDT"),
        _candidate("OTHERUSDT"),
        _candidate("BESTUSDT", score=0.95),
        _candidate("GOODUSDT", score=0.9),
        _candidate("THIRDUSDT", score=0.85),
        _candidate("SMALLUSDT", Tier.SMALL, score=0.99),
        _candidate("VETOUSDT", opinion=MarketOpinion(D("0.9"), D("0.9"), veto=True)),
    ]
    result = _plan(holdings=holdings, candidates=candidates)
    assert [i.symbol for i in result.ideas] == ["BESTUSDT", "GOODUSDT"]  # 3 vagas − 1 ocupada
    reasons = dict(result.rejections)
    assert reasons == {
        "SMALLUSDT": "tamanho abaixo do mínimo (orçamento/tier/risco)",
        "THIRDUSDT": "sem vagas no perfil",
        "VETOUSDT": "veto do analista",
        "HELDUSDT": "ativo já em carteira",
        "OTHERUSDT": "ativo já em carteira",
        "LOWUSDT": "score abaixo do mínimo",
        "NOSETUSDT": "sem setup",
    }


def test_positions_of_other_profiles_allowed_when_not_exclusive() -> None:
    holdings = [Holding("moderado", "AAAUSDT", Tier.CORE, D("50"))]
    result = _plan(
        holdings=holdings, candidates=[_candidate("AAAUSDT")], one_position_per_asset=False
    )
    assert [i.symbol for i in result.ideas] == ["AAAUSDT"]


def test_budget_and_tier_room_cap_the_size() -> None:
    holdings = [Holding("conservador", "HELDUSDT", Tier.LARGE, D("150"))]
    result = _plan(holdings=holdings, candidates=[_candidate("AAAUSDT", Tier.LARGE, stop_pct=0.01)])
    [idea] = result.ideas
    assert idea.notional <= D("50")  # tier large: 20% de 1000 = 200 − 150 em uso


def test_exposure_multiplier_scales_the_size_not_the_slots() -> None:
    """A cautela do analista reduz o tamanho de cada posição, e não o número de vagas (D-030):
    nas duas, ela contava duas vezes, e com 2 vagas qualquer leitura abaixo de 1,0 deixava 1."""
    candidates = [_candidate("AAAUSDT", score=0.9), _candidate("BBBUSDT", score=0.8)]
    result = _plan(candidates=candidates, exposure_multiplier=D("0.5"))
    assert [i.symbol for i in result.ideas] == ["AAAUSDT", "BBBUSDT"]  # as 3 vagas continuam
    assert result.ideas[0].notional == pytest.approx(D("125"), rel=D("0.01"))  # 250 × 0,5


def test_exposure_multiplier_also_scales_a_capped_size() -> None:
    """Com o tamanho limitado pelo tier (e não pelo risco), a cautela continua valendo."""
    holdings = [Holding("conservador", "HELDUSDT", Tier.LARGE, D("150"))]
    candidate = _candidate("AAAUSDT", Tier.LARGE, stop_pct=0.01)
    full = _plan(holdings=holdings, candidates=[candidate])
    half = _plan(holdings=holdings, candidates=[candidate], exposure_multiplier=D("0.5"))
    assert half.ideas[0].notional == pytest.approx(full.ideas[0].notional / 2, rel=D("0.01"))


def test_zero_exposure_opens_nothing() -> None:
    """Leitura que zera a exposição, ou on_failure: pause_entries sem leitura válida."""
    result = _plan(candidates=[_candidate("AAAUSDT")], exposure_multiplier=D(0))
    assert result.ideas == () and dict(result.rejections) == {"AAAUSDT": "exposição zero"}


def test_llm_opinion_blends_score_when_confident() -> None:
    confident = _candidate("AAAUSDT", score=0.7, opinion=MarketOpinion(D("-1"), D("0.9")))
    doubtful = _candidate("BBBUSDT", score=0.7, opinion=MarketOpinion(D("-1"), D("0.1")))
    assert confident.final_score(CONSERVATIVE) == D("0.8") * D("0.7") + D("0.2") * D("-0.9")
    assert doubtful.final_score(CONSERVATIVE) == D("0.7")  # confiança abaixo do mínimo
    result = _plan(candidates=[confident, doubtful])
    assert [i.symbol for i in result.ideas] == ["BBBUSDT"]


def test_maker_entry_uses_bid() -> None:
    maker = CONSERVATIVE.model_copy(
        update={"entry": CONSERVATIVE.entry.model_copy(update={"order": EntryMode.LIMIT_MAKER_GTC})}
    )
    [idea] = _plan(profile=maker, candidates=[_candidate("AAAUSDT", price="100")]).ideas
    assert idea.entry.limit_price == D("100")
    assert idea.entry.mode is EntryMode.LIMIT_MAKER_GTC


# ============================================================================ saídas
def _signal(score: float) -> Signal:
    return Signal(score=score, setup=None, stop_pct=0.02, atr_pct=0.01, close=100.0)


def test_weak_cycles_counter() -> None:
    assert next_weak_cycles(0, _signal(-0.5), CONSERVATIVE) == 1
    assert next_weak_cycles(1, _signal(-0.2), CONSERVATIVE) == 2
    assert next_weak_cycles(3, _signal(0.1), CONSERVATIVE) == 0
    assert next_weak_cycles(3, None, CONSERVATIVE) == 0


@pytest.mark.parametrize(
    ("age", "weak", "veto", "expected"),
    [
        (timedelta(days=1), 0, True, "veto do analista"),
        (timedelta(days=22), 0, False, "tempo máximo de permanência"),
        (timedelta(days=1), 2, False, "rotação: score no nível de saída"),
        (timedelta(days=1), 1, False, None),
        (None, 0, False, None),
    ],
)
def test_exit_reason(age: timedelta | None, weak: int, veto: bool, expected: str | None) -> None:
    assert exit_reason(profile=CONSERVATIVE, age=age, weak_cycles=weak, veto=veto) == expected


def test_exit_reason_without_max_holding() -> None:
    profile = CONSERVATIVE.model_copy(
        update={"protection": CONSERVATIVE.protection.model_copy(update={"max_holding": None})}
    )
    assert exit_reason(profile=profile, age=timedelta(days=999), weak_cycles=0) is None


def _position(entry: str | None = "100", stop_pct: str | None = "4") -> Position:
    policy = ProtectionPolicy(
        TakeProfitMode.TRAILING,
        D("3"),
        StopMode.FIXED if stop_pct else StopMode.TRAILING,
        take_profit_trailing_bips=100,
        stop_pct=D(stop_pct) if stop_pct else None,
        stop_trailing_bips=None if stop_pct else 300,
    )
    return Position(
        id=1,
        profile="con",
        decision_id="abcdef1234",
        symbol="SOLUSDT",
        base_asset="SOL",
        quote_asset="USDT",
        state=PositionState.PROTECTED,
        entry_mode=EntryMode.LIMIT_FOK,
        policy=policy,
        planned_qty=D(1),
        planned_price=D(100),
        protection_list_id="x",
        entry_price=D(entry) if entry else None,
    )


def test_break_even_after_one_r() -> None:
    assert break_even_protection(_position(), CONSERVATIVE, D("103.9")) is None  # < 1R
    protection = break_even_protection(_position(), CONSERVATIVE, D("104"))
    assert protection is not None
    assert protection.stop == FixedStop(D("100.3"))
    assert protection.take_profit == TrailingTakeProfit(
        D("104") * D("1.003"), 100
    )  # ativação já passou


def test_break_even_keeps_activation_when_still_ahead() -> None:
    wide = CONSERVATIVE.model_copy(
        update={
            "protection": CONSERVATIVE.protection.model_copy(update={"break_even_after_r": 0.5})
        }
    )
    protection = break_even_protection(_position(), wide, D("102.5"))
    assert protection is not None
    assert protection.take_profit == TrailingTakeProfit(D("103"), 100)


def test_break_even_not_applicable() -> None:
    no_trigger = MODERATE.model_copy(
        update={"protection": MODERATE.protection.model_copy(update={"break_even_after_r": None})}
    )
    assert break_even_protection(_position(), no_trigger, D("200")) is None
    assert break_even_protection(_position(entry=None), CONSERVATIVE, D("200")) is None
    assert break_even_protection(_position(stop_pct=None), CONSERVATIVE, D("200")) is None
    tiny_fee = break_even_protection(_position(), CONSERVATIVE, D("104"), fee_buffer=D("0.05"))
    assert tiny_fee is None  # stop proposto acima do preço atual
    negative = break_even_protection(_position(), CONSERVATIVE, D("104"), fee_buffer=D("-0.05"))
    assert negative is None  # não piora o stop existente
