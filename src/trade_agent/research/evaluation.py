"""Evaluation of the analyst against labeled cases (Phase 4 exit criterion).

Each case describes a situation (historical or synthetic) with news, metrics and
candidates, and the expectations: assets that **must** be vetoed, assets that **must not**
be vetoed, accepted regimes, exposure range and sentiment sign.

The evaluation runs only the structured step, **without web search**: in historical
cases, the search would bring back the known outcome (hindsight bias). Even so, the model
may know the historical events from its training; that is why there are also synthetic
cases, with fictional assets.

Exit criterion: 100% of the answers valid against the schema, every critical case correct
and the projected daily cost within the budget.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from trade_agent.research.analyst import MarketAnalyst
from trade_agent.research.llm import BudgetExceededError, LlmError, LlmOutputError, TrackingLedger
from trade_agent.research.models import (
    CandidateContext,
    FearGreed,
    MarketMetrics,
    MarketRegime,
    MarketView,
    NewsItem,
    StoredNews,
)

CYCLES_PER_DAY = 6


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvalNews(_Strict):
    source: str
    title: str
    summary: str = ""
    url: str | None = None
    hours_ago: float = Field(default=2, ge=0)


class EvalCandidate(_Strict):
    asset: str
    tier: str = "large"
    ta_score: float = 0.6
    setup: str | None = "trend_pullback"
    held: bool = False


class EvalExpectation(_Strict):
    veto: list[str] = Field(default_factory=list)
    no_veto: list[str] = Field(default_factory=list)
    regime: list[MarketRegime] = Field(default_factory=list)
    max_exposure: float | None = None
    min_exposure: float | None = None
    negative: list[str] = Field(default_factory=list)
    """Sentiment ≤ 0 (or a veto)."""
    positive: list[str] = Field(default_factory=list)
    """Sentiment > 0 and no veto."""
    max_sentiment: dict[str, float] = Field(default_factory=dict)


class EvalCase(_Strict):
    id: str
    kind: Literal["historical", "synthetic"]
    critical: bool = False
    description: str
    as_of: datetime
    fear_greed: int | None = Field(default=None, ge=0, le=100)
    btc_change_24h: float | None = None
    candidates: list[EvalCandidate]
    news: list[EvalNews]
    expect: EvalExpectation


def load_cases(path: Path | str) -> list[EvalCase]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    cases = [EvalCase.model_validate(c) for c in data["cases"]]
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("ids de casos repetidos")
    return cases


def fear_greed_label(value: int) -> str:
    for limit, label in ((25, "Extreme Fear"), (45, "Fear"), (55, "Neutral"), (75, "Greed")):
        if value < limit:
            return label
    return "Extreme Greed"


def case_inputs(
    case: EvalCase,
) -> tuple[MarketMetrics, list[CandidateContext], list[StoredNews]]:
    metrics = MarketMetrics(
        fear_greed=(
            FearGreed(case.fear_greed, fear_greed_label(case.fear_greed))
            if case.fear_greed is not None
            else None
        ),
        btc_change_24h=case.btc_change_24h,
    )
    candidates = [
        CandidateContext(
            asset=c.asset,
            symbol=f"{c.asset}USDT",
            tier=c.tier,
            ta_score=c.ta_score,
            setup=c.setup,
            held=c.held,
        )
        for c in case.candidates
    ]
    news = [
        StoredNews(
            id=index,
            item=NewsItem(
                source=n.source,
                title=n.title,
                summary=n.summary,
                url=n.url,
                published_at=case.as_of - timedelta(hours=n.hours_ago),
            ),
        )
        for index, n in enumerate(case.news, start=1)
    ]
    return metrics, candidates, news


def check(case: EvalCase, view: MarketView) -> list[str]:
    """Unmet expectations (empty list = case passed)."""
    expect = case.expect
    failures: list[str] = []

    def vetoed(asset: str) -> bool:
        reading = view.asset(asset)
        return reading is not None and reading.veto

    def sentiment(asset: str) -> float:
        reading = view.asset(asset)
        return reading.sentiment if reading is not None else 0.0

    failures += [f"{a}: veto esperado" for a in expect.veto if not vetoed(a)]
    failures += [f"{a}: veto indevido" for a in expect.no_veto if vetoed(a)]
    if expect.regime and view.market_regime not in expect.regime:
        failures.append(f"regime {view.market_regime} fora de {[r.value for r in expect.regime]}")
    if expect.max_exposure is not None and view.exposure_multiplier > expect.max_exposure:
        failures.append(f"exposição {view.exposure_multiplier} > {expect.max_exposure}")
    if expect.min_exposure is not None and view.exposure_multiplier < expect.min_exposure:
        failures.append(f"exposição {view.exposure_multiplier} < {expect.min_exposure}")
    failures += [
        f"{a}: sentimento {sentiment(a):+.2f} deveria ser ≤ 0"
        for a in expect.negative
        if not vetoed(a) and sentiment(a) > 0
    ]
    failures += [
        f"{a}: sentimento {sentiment(a):+.2f} deveria ser > 0 sem veto"
        for a in expect.positive
        if vetoed(a) or sentiment(a) <= 0
    ]
    failures += [
        f"{a}: sentimento {sentiment(a):+.2f} acima de {limit:+.2f}"
        for a, limit in expect.max_sentiment.items()
        if sentiment(a) > limit
    ]
    return failures


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    case_id: str
    critical: bool
    schema_ok: bool
    failures: tuple[str, ...]
    cost_usd: Decimal
    error: str | None = None
    view: MarketView | None = None

    @property
    def passed(self) -> bool:
        return self.schema_ok and self.error is None and not self.failures


@dataclass(frozen=True, slots=True)
class EvalReport:
    outcomes: tuple[CaseOutcome, ...]
    budget_usd: Decimal
    skipped: tuple[str, ...] = field(default_factory=tuple)
    """Cases not run (the evaluation budget ran out)."""

    @property
    def schema_rate(self) -> float:
        ran = [o for o in self.outcomes if o.error is None or not o.schema_ok]
        return sum(o.schema_ok for o in ran) / len(ran) if ran else 0.0

    @property
    def critical_ok(self) -> bool:
        return all(o.passed for o in self.outcomes if o.critical)

    @property
    def mean_cost(self) -> Decimal:
        if not self.outcomes:
            return Decimal(0)
        return sum((o.cost_usd for o in self.outcomes), Decimal(0)) / len(self.outcomes)

    @property
    def projected_daily_cost(self) -> Decimal:
        return self.mean_cost * CYCLES_PER_DAY

    @property
    def passed(self) -> bool:
        return (
            not self.skipped
            and bool(self.outcomes)
            and all(o.schema_ok for o in self.outcomes)
            and self.critical_ok
            and self.projected_daily_cost <= self.budget_usd
        )


async def run_eval(
    analyst: MarketAnalyst,
    ledger: TrackingLedger,
    cases: Sequence[EvalCase],
    *,
    budget_usd: Decimal,
) -> EvalReport:
    outcomes: list[CaseOutcome] = []
    for index, case in enumerate(cases):
        metrics, candidates, news = case_inputs(case)
        before = ledger.total
        try:
            analysis = await analyst.analyze(
                as_of=case.as_of, metrics=metrics, candidates=candidates, news=news, web=False
            )
        except BudgetExceededError:
            skipped = tuple(c.id for c in cases[index:])
            return EvalReport(tuple(outcomes), budget_usd, skipped)
        except LlmOutputError as exc:
            outcomes.append(
                CaseOutcome(case.id, case.critical, False, (), ledger.total - before, str(exc))
            )
            continue
        except LlmError as exc:
            outcomes.append(
                CaseOutcome(case.id, case.critical, True, (), ledger.total - before, str(exc))
            )
            continue
        outcomes.append(
            CaseOutcome(
                case_id=case.id,
                critical=case.critical,
                schema_ok=True,
                failures=tuple(check(case, analysis.view)),
                cost_usd=ledger.total - before,
                view=analysis.view,
            )
        )
    return EvalReport(tuple(outcomes), budget_usd)


def render_report(report: EvalReport, *, model: str) -> str:
    lines = [
        f"# Avaliação do analista — `{model}`",
        "",
        "| Caso | Crítico | Schema | Resultado | Custo US$ | Falhas |",
        "|------|:-------:|:------:|:---------:|----------:|--------|",
    ]
    for o in report.outcomes:
        status = "✅" if o.passed else "❌"
        detail = "; ".join(o.failures) or (o.error or "")
        lines.append(
            f"| {o.case_id} | {'sim' if o.critical else ''} | {'ok' if o.schema_ok else 'inválido'}"
            f" | {status} | {o.cost_usd:.4f} | {detail} |"
        )
    passed = sum(o.passed for o in report.outcomes)
    lines += [
        "",
        f"- Casos aprovados: **{passed}/{len(report.outcomes)}**",
        f"- Saídas válidas no schema: **{report.schema_rate:.0%}**",
        f"- Casos críticos corretos: **{'sim' if report.critical_ok else 'não'}**",
        f"- Custo médio por ciclo: **US$ {report.mean_cost:.4f}**; projeção de "
        f"{CYCLES_PER_DAY} ciclos/dia: **US$ {report.projected_daily_cost:.2f}** "
        f"(orçamento US$ {report.budget_usd})",
    ]
    if report.skipped:
        lines.append(f"- **Não executados** (orçamento esgotado): {', '.join(report.skipped)}")
    verdict = "atendido ✅" if report.passed else "não atendido ❌"
    lines += ["", f"**Critério de saída da Fase 4:** {verdict}"]
    return "\n".join(lines) + "\n"
