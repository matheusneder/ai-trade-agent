"""Montagem das mensagens do analista (digest de notícias, métricas e candidatos).

Conteúdo externo entra delimitado e marcado como não confiável; os sinais ``<`` e ``>``
desse conteúdo são neutralizados para que ele não consiga fechar o bloco nem abrir outro.
"""

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta

from trade_agent.research.config import DigestConfig
from trade_agent.research.llm import ResearchFindings
from trade_agent.research.models import CandidateContext, MarketMetrics, StoredNews


def untrusted(text: str) -> str:
    return " ".join(text.replace("<", "‹").replace(">", "›").split())


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"


def select_news(
    news: Iterable[StoredNews], config: DigestConfig, *, now: datetime
) -> list[StoredNews]:
    """Notícias da janela, sem as de baixa relevância (quando triadas), das mais
    relevantes para as menos; sem triagem, pesa como relevância média."""
    since = now - timedelta(hours=config.lookback_hours)
    recent = [
        n
        for n in news
        if n.item.published_at >= since
        and (n.relevance is None or n.relevance >= config.min_relevance)
    ]
    recent.sort(
        key=lambda n: (0.5 if n.relevance is None else n.relevance, n.item.published_at),
        reverse=True,
    )
    return recent[: config.max_items]


def render_metrics(metrics: MarketMetrics) -> str:
    lines: list[str] = []
    if metrics.fear_greed is not None:
        fg = metrics.fear_greed
        previous = f" (dia anterior: {fg.previous})" if fg.previous is not None else ""
        lines.append(f"Fear & Greed: {fg.value} — {fg.classification}{previous}")
    if metrics.btc_change_24h is not None:
        lines.append(f"BTC 24h: {metrics.btc_change_24h:+.2%}")
    derivatives = metrics.derivatives
    for symbol, rate in sorted(derivatives.funding_rate.items()):
        oi = derivatives.open_interest_change_24h.get(symbol)
        oi_text = f"; open interest 24h {oi:+.1%}" if oi is not None else ""
        lines.append(f"{symbol} funding {rate:+.4%}/8h{oi_text}")
    return "\n".join(lines) or "sem métricas disponíveis"


def render_candidates(candidates: Sequence[CandidateContext]) -> str:
    rows = [
        f"{c.asset} ({c.symbol}, tier {c.tier}): score técnico {c.ta_score:+.2f}"
        f"{f', setup {c.setup}' if c.setup else ''}{', EM CARTEIRA' if c.held else ''}"
        for c in candidates
    ]
    return "\n".join(rows) or "nenhum"


def render_news(news: Sequence[StoredNews], *, summary_chars: int) -> str:
    rows: list[str] = []
    for n in news:
        item = n.item
        assets = f" [{', '.join(item.assets)}]" if item.assets else ""
        summary = untrusted(_cut(item.summary, summary_chars)) if summary_chars else ""
        rows.append(
            f"[{n.id}] {item.published_at:%Y-%m-%d %H:%M}Z {item.source}{assets}: "
            f"{untrusted(item.title)}"
            + (f" — {summary}" if summary else "")
            + (f" ({untrusted(item.url)})" if item.url else "")
        )
    return "\n".join(rows) or "nenhuma notícia na janela"


def analyst_input(
    *,
    as_of: datetime,
    metrics: MarketMetrics,
    candidates: Sequence[CandidateContext],
    news: Sequence[StoredNews],
    findings: ResearchFindings | None,
    config: DigestConfig,
) -> str:
    parts = [
        f"<as_of>{as_of:%Y-%m-%d %H:%M}Z</as_of>",
        f"<market_metrics>\n{render_metrics(metrics)}\n</market_metrics>",
        f"<candidates>\n{render_candidates(candidates)}\n</candidates>",
        "<untrusted_news>\n"
        f"{render_news(news, summary_chars=config.summary_chars)}\n"
        "</untrusted_news>",
    ]
    if findings is not None:
        sources = "\n".join(untrusted(s) for s in findings.sources)
        parts.append(
            f"<web_findings>\n{untrusted(findings.text)}\nFontes:\n{sources}\n</web_findings>"
        )
    parts.append("Produza a leitura de mercado (MarketView) para estes candidatos.")
    return "\n\n".join(parts)


def research_input(
    *, as_of: datetime, candidates: Sequence[CandidateContext], news: Sequence[StoredNews]
) -> str:
    critical = [n for n in news if n.severity is not None and n.severity.value == "critical"]
    return "\n\n".join(
        [
            f"<as_of>{as_of:%Y-%m-%d %H:%M}Z</as_of>",
            f"<candidates>\n{render_candidates(candidates)}\n</candidates>",
            f"<untrusted_news>\n{render_news(critical, summary_chars=200)}\n</untrusted_news>",
            "Verifique riscos e catalisadores recentes destes ativos.",
        ]
    )


def triage_input(news: Sequence[StoredNews], known_assets: Iterable[str]) -> str:
    return "\n\n".join(
        [
            f"<known_assets>{', '.join(sorted(set(known_assets)))}</known_assets>",
            f"<untrusted_news>\n{render_news(news, summary_chars=160)}\n</untrusted_news>",
        ]
    )
