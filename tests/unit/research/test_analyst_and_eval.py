from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.support.claude import NOW, FakeClaude, research_config
from trade_agent.research.analyst import MarketAnalyst
from trade_agent.research.config import BudgetConfig, WebResearchConfig
from trade_agent.research.evaluation import (
    CaseOutcome,
    EvalCase,
    EvalReport,
    case_inputs,
    check,
    fear_greed_label,
    load_cases,
    render_report,
    run_eval,
)
from trade_agent.research.llm import BudgetExceededError, MemoryLedger, TrackingLedger
from trade_agent.research.models import (
    AssetView,
    CandidateContext,
    Horizon,
    MarketMetrics,
    MarketRegime,
    MarketView,
    NewsItem,
    StoredNews,
)

CASES_FILE = Path(__file__).parents[3] / "evals" / "analyst" / "cases.yaml"
CANDIDATES = [
    CandidateContext("SOL", "SOLUSDT", "large", 0.6),
    CandidateContext("BTC", "BTCUSDT", "core", 0.4),
]


def _draft(
    *assets: dict[str, Any], exposure: float = 0.8, regime: str = "neutral"
) -> dict[str, Any]:
    return {
        "market_regime": regime,
        "global_sentiment": 0.0,
        "exposure_multiplier": exposure,
        "global_risk_flags": [],
        "assets": list(assets),
    }


def _asset(code: str, sentiment: float = 0.2, veto: bool = False) -> dict[str, Any]:
    return {
        "asset": code, "sentiment": sentiment, "confidence": 0.7, "horizon": "days",
        "catalysts": [], "risk_flags": [], "veto": veto, "rationale": "r", "sources": [],
    }  # fmt: skip


def _stored(news_id: int, title: str = "t") -> StoredNews:
    return StoredNews(news_id, NewsItem("rss", title, NOW - timedelta(hours=1)))


async def test_triage_maps_known_ids_and_assets() -> None:
    fake = FakeClaude()
    fake.reply_json(
        {
            "items": [
                {
                    "id": 1,
                    "relevance": 1.4,
                    "category": "hack",
                    "severity": "critical",
                    "assets": ["sol", "XYZ"],
                },
                {"id": 99, "relevance": 0.5, "category": "other", "severity": "low", "assets": []},
            ]
        }
    )
    analyst = MarketAnalyst(fake.claude(), research_config())
    assert await analyst.triage([], ["SOL"]) == {}
    triage = await analyst.triage([_stored(1)], ["sol"])
    assert list(triage) == [1]
    assert triage[1].relevance == 1.0
    assert triage[1].assets == ("SOL",)
    assert fake.requests[0]["model"] == "claude-sonnet-5"


async def test_analyze_with_web_research_and_safety() -> None:
    fake = FakeClaude()
    fake.reply(
        [{"type": "text", "text": "achados", "citations": None}],
    )
    fake.reply_json(_draft(_asset("SOL", veto=True), _asset("DOGE")))
    analyst = MarketAnalyst(fake.claude(), research_config())
    analysis = await analyst.analyze(
        as_of=NOW, metrics=MarketMetrics(), candidates=CANDIDATES, news=[_stored(1)], web=True
    )
    assert analysis.findings is not None and analysis.findings.text == "achados"
    assert [a.asset for a in analysis.view.assets] == ["SOL"]
    assert analysis.adjustments == ("DOGE: fora do universo recebido, descartado",)
    assert analysis.notes == ()
    research, structured = fake.requests
    assert "tools" in research and "tools" not in structured
    assert "<web_findings>" in structured["messages"][0]["content"]


async def test_analyze_skips_or_survives_web_research() -> None:
    fake = FakeClaude()
    fake.reply_json(_draft())  # web=False
    fake.reply_json(_draft())  # web desabilitado na configuração
    fake.reply_json(_draft())  # sem candidatos
    fake.fail(500)  # pesquisa falha...
    fake.reply_json(_draft())  # ...e a leitura segue sem ela
    enabled = MarketAnalyst(fake.claude(), research_config())
    disabled = MarketAnalyst(fake.claude(), research_config(web=WebResearchConfig(enabled=False)))
    args: dict[str, Any] = {"as_of": NOW, "metrics": MarketMetrics(), "news": []}
    assert (await enabled.analyze(candidates=CANDIDATES, web=False, **args)).findings is None
    assert (await disabled.analyze(candidates=CANDIDATES, web=True, **args)).findings is None
    assert (await enabled.analyze(candidates=[], web=True, **args)).findings is None
    survived = await enabled.analyze(candidates=CANDIDATES, web=True, **args)
    assert survived.findings is None
    assert survived.notes[0].startswith("pesquisa web indisponível")
    assert all("tools" not in r for r in fake.requests[:3])


async def test_budget_exhausted_during_research_propagates() -> None:
    ledger = MemoryLedger()
    config = research_config(budget=BudgetConfig(daily_usd=Decimal("0.001")))
    fake = FakeClaude()
    fake.reply([{"type": "text", "text": "x"}])  # pesquisa consome o orçamento
    analyst = MarketAnalyst(fake.claude(config=config, ledger=ledger), config)
    with pytest.raises(BudgetExceededError):
        await analyst.analyze(
            as_of=NOW, metrics=MarketMetrics(), candidates=CANDIDATES, news=[], web=True
        )
    await ledger.record(ledger.usages[0])
    analyst_budget_gone = MarketAnalyst(fake.claude(config=config, ledger=ledger), config)
    with pytest.raises(BudgetExceededError):
        await analyst_budget_gone.analyze(
            as_of=NOW, metrics=MarketMetrics(), candidates=CANDIDATES, news=[], web=True
        )


# ============================================================================ avaliação
def test_repository_cases_are_valid() -> None:
    cases = load_cases(CASES_FILE)
    assert len(cases) >= 30
    assert sum(c.kind == "synthetic" for c in cases) >= 10
    assert sum(c.critical for c in cases) >= 15
    for case in cases:
        referenced = set(case.expect.veto + case.expect.no_veto + case.expect.negative)
        referenced |= set(case.expect.positive) | set(case.expect.max_sentiment)
        assert referenced <= {c.asset for c in case.candidates}, case.id


def test_duplicate_case_ids_are_rejected(tmp_path: Path) -> None:
    case = {
        "id": "a", "kind": "synthetic", "description": "d", "as_of": "2026-01-01T00:00:00Z",
        "candidates": [], "news": [], "expect": {},
    }  # fmt: skip
    path = tmp_path / "cases.yaml"
    path.write_text(__import__("yaml").safe_dump({"cases": [case, case]}), encoding="utf-8")
    with pytest.raises(ValueError, match="repetidos"):
        load_cases(path)


@pytest.mark.parametrize(
    ("value", "label"),
    [
        (0, "Extreme Fear"),
        (24, "Extreme Fear"),
        (25, "Fear"),
        (50, "Neutral"),
        (74, "Greed"),
        (75, "Extreme Greed"),
    ],
)
def test_fear_greed_label(value: int, label: str) -> None:
    assert fear_greed_label(value) == label


def _case(**expect: Any) -> EvalCase:
    return EvalCase.model_validate(
        {
            "id": "c", "kind": "synthetic", "critical": True, "description": "d",
            "as_of": NOW, "fear_greed": 20, "btc_change_24h": -0.1,
            "candidates": [{"asset": "SOL", "held": True}, {"asset": "BTC", "tier": "core"}],
            "news": [{"source": "rss", "title": "t", "hours_ago": 3}],
            "expect": expect,
        }
    )  # fmt: skip


def test_case_inputs() -> None:
    metrics, candidates, news = case_inputs(_case())
    assert metrics.fear_greed is not None and metrics.fear_greed.classification == "Extreme Fear"
    assert metrics.btc_change_24h == -0.1
    assert [(c.symbol, c.held) for c in candidates] == [("SOLUSDT", True), ("BTCUSDT", False)]
    assert news[0].id == 1 and news[0].item.published_at == NOW - timedelta(hours=3)
    no_fg = case_inputs(_case().model_copy(update={"fear_greed": None}))[0]
    assert no_fg.fear_greed is None


def _view(**overrides: Any) -> MarketView:
    data: dict[str, Any] = {
        "as_of": NOW,
        "market_regime": MarketRegime.NEUTRAL,
        "global_sentiment": 0,
        "exposure_multiplier": 0.7,
        "assets": (
            AssetView(asset="SOL", sentiment=0.4, confidence=0.5, horizon=Horizon.DAYS),
            AssetView(asset="BTC", sentiment=-0.3, confidence=0.5, horizon=Horizon.DAYS, veto=True),
        ),
    }
    data.update(overrides)
    return MarketView(**data)


def test_check_each_expectation() -> None:
    view = _view()
    assert check(_case(), view) == []
    assert check(
        _case(
            veto=["SOL"],
            no_veto=["BTC"],
            regime=["risk_off"],
            max_exposure=0.5,
            min_exposure=0.8,
            negative=["SOL"],
            positive=["BTC", "ETH"],
            max_sentiment={"SOL": 0.2},
        ),
        view,
    ) == [
        "SOL: veto esperado",
        "BTC: veto indevido",
        "regime neutral fora de ['risk_off']",
        "exposição 0.7 > 0.5",
        "exposição 0.7 < 0.8",
        "SOL: sentimento +0.40 deveria ser ≤ 0",
        "BTC: sentimento -0.30 deveria ser > 0 sem veto",
        "ETH: sentimento +0.00 deveria ser > 0 sem veto",
        "SOL: sentimento +0.40 acima de +0.20",
    ]
    assert check(_case(negative=["BTC", "ETH"], positive=["SOL"]), view) == []


async def test_run_eval_outcomes_and_report() -> None:
    fake = FakeClaude()
    fake.reply_json(_draft(_asset("SOL", veto=True)))  # aprovado
    fake.reply([{"type": "text", "text": "não é json"}])  # schema inválido
    fake.fail(500)  # erro da API
    fake.reply_json(_draft(_asset("SOL")))  # expectativa falha
    ledger = TrackingLedger(MemoryLedger())
    analyst = MarketAnalyst(fake.claude(ledger=ledger), research_config())
    cases = [_case(veto=["SOL"]).model_copy(update={"id": f"c{i}"}) for i in range(4)]
    report = await run_eval(analyst, ledger, cases, budget_usd=Decimal(5))
    assert [o.passed for o in report.outcomes] == [True, False, False, False]
    assert [o.schema_ok for o in report.outcomes] == [True, False, True, True]
    assert report.outcomes[3].failures == ("SOL: veto esperado",)
    assert report.schema_rate == pytest.approx(2 / 3)
    assert not report.critical_ok and not report.passed
    assert report.mean_cost > 0
    assert report.projected_daily_cost == report.mean_cost * 6
    text = render_report(report, model="claude-opus-5")
    assert "| c0 | sim | ok | ✅ |" in text
    assert "| c1 | sim | inválido | ❌ |" in text
    assert "InternalServerError" in text
    assert "Casos aprovados: **1/4**" in text
    assert "não atendido ❌" in text


async def test_run_eval_stops_when_budget_is_exhausted() -> None:
    config = research_config(budget=BudgetConfig(daily_usd=Decimal("0.001")))
    fake = FakeClaude()
    fake.reply_json(_draft(_asset("SOL", veto=True)))
    ledger = TrackingLedger(MemoryLedger())
    analyst = MarketAnalyst(fake.claude(config=config, ledger=ledger), config)
    cases = [_case(veto=["SOL"]).model_copy(update={"id": f"c{i}"}) for i in range(3)]
    report = await run_eval(analyst, ledger, cases, budget_usd=Decimal(5))
    assert [o.case_id for o in report.outcomes] == ["c0"]
    assert report.skipped == ("c1", "c2")
    assert not report.passed
    assert "Não executados" in render_report(report, model="m")


def test_report_properties_edge_cases() -> None:
    empty = EvalReport((), Decimal(5))
    assert empty.schema_rate == 0.0 and empty.mean_cost == 0 and not empty.passed
    ok = CaseOutcome("a", True, True, (), Decimal("0.1"))
    good = EvalReport((ok,), Decimal(5))
    assert good.passed and good.critical_ok
    assert "atendido ✅" in render_report(good, model="m")
    expensive = EvalReport((ok,), Decimal("0.5"))
    assert not expensive.passed
