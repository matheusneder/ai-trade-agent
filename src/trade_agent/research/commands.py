"""``trade-agent research ...`` subcommands (market analyst).

Examples::

    trade-agent research ingest                     # collects news and metrics (database)
    trade-agent research run --assets BTC,ETH,SOL   # one research cycle (database + key)
    trade-agent research show                       # latest valid reading
    trade-agent research eval --limit 5             # evaluation with labeled cases (key)
"""

import argparse
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO

import anthropic
import httpx

from trade_agent.config.settings import Settings
from trade_agent.persistence.db import Database
from trade_agent.persistence.migrate import upgrade_to_head
from trade_agent.persistence.research_store import ResearchStore
from trade_agent.research.analyst import MarketAnalyst
from trade_agent.research.collector import NewsCollector
from trade_agent.research.config import ResearchConfig, load_research_config
from trade_agent.research.evaluation import load_cases, render_report, run_eval
from trade_agent.research.llm import ClaudeClient, MemoryLedger, TrackingLedger
from trade_agent.research.models import CandidateContext
from trade_agent.research.service import ResearchService
from trade_agent.research.tagging import AssetTagger

DEFAULT_CASES = Path("evals/analyst/cases.yaml")
DEFAULT_OUTPUT = Path("var/eval")


class CommandError(Exception):
    """Unmet precondition (message for the operator)."""


@dataclass(frozen=True, slots=True)
class ResearchDeps:
    anthropic_factory: Callable[[Settings], anthropic.AsyncAnthropic]
    http_factory: Callable[[ResearchConfig], httpx.AsyncClient]
    database_factory: Callable[[Settings], Database]
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


def _anthropic(settings: Settings) -> anthropic.AsyncAnthropic:
    if settings.anthropic_api_key is None:
        raise CommandError("defina ANTHROPIC_API_KEY no .env")
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())


def _http(config: ResearchConfig) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=config.sources.timeout_s)


def _database(settings: Settings) -> Database:
    if settings.database_url is None:
        raise CommandError("defina TA_DATABASE_URL no .env")
    return Database(settings.database_url.get_secret_value())


DEFAULT_DEPS = ResearchDeps(_anthropic, _http, _database)


def add_research_parser(commands: Any) -> None:
    research = commands.add_parser("research", help="analista de mercado (notícias + LLM)")
    sub = research.add_subparsers(dest="research_command", required=True)
    sub.add_parser("ingest", help="coleta notícias e métricas e grava no banco")
    run = sub.add_parser("run", help="executa um ciclo de pesquisa")
    run.add_argument("--assets", required=True, help="ativos candidatos, ex.: BTC,ETH,SOL")
    run.add_argument("--no-web", action="store_true", help="sem a etapa de busca web")
    sub.add_parser("show", help="última leitura válida (MarketView)")
    evaluate = sub.add_parser("eval", help="avaliação com casos rotulados")
    evaluate.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    evaluate.add_argument("--case", action="append", help="roda só estes ids (repetível)")
    evaluate.add_argument("--limit", type=int, help="roda só os N primeiros casos")
    evaluate.add_argument("--budget", type=Decimal, help="teto da avaliação (US$)")
    evaluate.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)


def _emit(out: TextIO, payload: Any) -> None:
    out.write(json.dumps(payload, indent=2, default=str, ensure_ascii=False) + "\n")


def _service(
    settings: Settings, config: ResearchConfig, deps: ResearchDeps, db: Database
) -> tuple[ResearchService, httpx.AsyncClient]:
    http = deps.http_factory(config)
    tagger = AssetTagger(config.asset_names, config.asset_names)
    client = deps.anthropic_factory(settings) if settings.anthropic_api_key else None
    service = ResearchService(
        store=ResearchStore(db),
        collector=NewsCollector(http, config.sources, tagger, deps.clock),
        client=client,
        config=config,
        clock=deps.clock,
    )
    return service, http


async def _with_database(
    args: argparse.Namespace,
    settings: Settings,
    config: ResearchConfig,
    deps: ResearchDeps,
    out: TextIO,
) -> int:
    if args.research_command == "run":
        deps.anthropic_factory(settings)  # fails early without the key
    db = deps.database_factory(settings)
    try:
        await upgrade_to_head(db.engine)
        service, http = _service(settings, config, deps, db)
        async with http:
            if args.research_command == "ingest":
                symbols = [f"{a}USDT" for a in config.asset_names]
                report = await service.ingest(derivative_symbols=symbols)
                _emit(
                    out,
                    {
                        "coletadas": report.collected,
                        "novas": report.new_items,
                        "erros": dict(report.errors),
                    },
                )
            elif args.research_command == "run":
                candidates = [
                    CandidateContext(asset=a, symbol=f"{a}USDT", tier="?", ta_score=0.0)
                    for a in (x.strip().upper() for x in args.assets.split(","))
                    if a
                ]
                await service.ingest(derivative_symbols=[c.symbol for c in candidates])
                result = await service.run_cycle(
                    trigger="manual", candidates=candidates, web=not args.no_web
                )
                _emit(
                    out,
                    {
                        "relatorio": result.report_id,
                        "custo_usd": result.cost_usd,
                        "erro": result.error,
                        "notas": list(result.notes),
                        "leitura": result.view.model_dump(mode="json") if result.view else None,
                    },
                )
                return 0 if result.ok else 1
            else:  # show
                view = await service.latest_view()
                _emit(out, view.model_dump(mode="json") if view else None)
    finally:
        await db.dispose()
    return 0


async def _evaluate(
    args: argparse.Namespace,
    settings: Settings,
    config: ResearchConfig,
    deps: ResearchDeps,
    out: TextIO,
) -> int:
    cases = load_cases(args.cases)
    if args.case:
        cases = [c for c in cases if c.id in set(args.case)]
    if args.limit is not None:
        cases = cases[: args.limit]
    if not cases:
        raise CommandError("nenhum caso selecionado")
    budget = args.budget or config.budget.daily_usd
    eval_config = config.model_copy(
        update={"budget": config.budget.model_copy(update={"daily_usd": budget})}
    )
    ledger = TrackingLedger(MemoryLedger())
    llm = ClaudeClient(deps.anthropic_factory(settings), eval_config, ledger, deps.clock)
    report = await run_eval(
        MarketAnalyst(llm, eval_config), ledger, cases, budget_usd=config.budget.daily_usd
    )
    text = render_report(report, model=config.models.analyst)
    args.output.mkdir(parents=True, exist_ok=True)
    stamp = deps.clock().strftime("%Y%m%d-%H%M%S")
    (args.output / f"analyst-{stamp}.md").write_text(text, encoding="utf-8")
    out.write(text)
    return 0 if report.passed else 1


async def run_research(
    args: argparse.Namespace,
    settings: Settings,
    out: TextIO,
    err: TextIO,
    deps: ResearchDeps = DEFAULT_DEPS,
) -> int:
    try:
        config = load_research_config(settings.research_config)
        if args.research_command == "eval":
            return await _evaluate(args, settings, config, deps, out)
        return await _with_database(args, settings, config, deps, out)
    except CommandError as exc:
        err.write(f"Erro: {exc}\n")
        return 2
