"""Regras de segurança do ``MarketView``, aplicadas por **código** (doc 03, §7.3).

* faixas numéricas truncadas (valores não finitos viram o valor mais conservador);
* **autoridade assimétrica**: ativos fora do universo recebido são descartados, e o
  multiplicador de exposição nunca passa de 1 (o LLM só reduz risco);
* sentimento acima de ``bullish_threshold`` exige ``min_bullish_sources`` fontes de
  domínios distintos; sem elas, é rebaixado ao limite. Vetos valem sem fonte;
* textos e listas são limitados (auditoria enxuta, sem conteúdo arbitrário longo).
"""

import math
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from trade_agent.research.config import SafetyConfig
from trade_agent.research.models import AssetDraft, AssetView, MarketView, MarketViewDraft


@dataclass(frozen=True, slots=True)
class SafetyResult:
    view: MarketView
    adjustments: tuple[str, ...]
    """Ajustes aplicados, para auditoria (ex.: "SOL: sentimento rebaixado ...")."""


def _clamp(value: float, low: float, high: float, fallback: float) -> float:
    if not math.isfinite(value):
        return fallback
    return min(max(value, low), high)


def _text(value: str, limit: int) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def _texts(values: Iterable[str], config: SafetyConfig) -> tuple[str, ...]:
    cleaned = [_text(v, config.max_text_chars) for v in values if v.strip()]
    return tuple(dict.fromkeys(cleaned))[: config.max_list_items]


def _urls(values: Iterable[str]) -> tuple[str, ...]:
    urls = (v.strip() for v in values)
    return tuple(dict.fromkeys(u for u in urls if urlsplit(u).scheme in {"http", "https"}))


def distinct_hosts(urls: Iterable[str]) -> int:
    hosts = {urlsplit(u).hostname or "" for u in urls}
    return len({h.removeprefix("www.") for h in hosts if h})


def _merge(first: AssetView, second: AssetView) -> AssetView:
    """Duas leituras do mesmo ativo: combina pelo lado mais conservador."""
    return first.model_copy(
        update={
            "sentiment": min(first.sentiment, second.sentiment),
            "confidence": min(first.confidence, second.confidence),
            "veto": first.veto or second.veto,
            "risk_flags": tuple(dict.fromkeys(first.risk_flags + second.risk_flags)),
            "sources": tuple(dict.fromkeys(first.sources + second.sources)),
        }
    )


def _asset_view(draft: AssetDraft, code: str, config: SafetyConfig, notes: list[str]) -> AssetView:
    sentiment = _clamp(draft.sentiment, -1.0, 1.0, 0.0)
    confidence = _clamp(draft.confidence, 0.0, 1.0, 0.0)
    if (sentiment, confidence) != (draft.sentiment, draft.confidence):
        notes.append(f"{code}: sentimento/confiança truncados à faixa")
    sources = _urls(draft.sources)
    if (
        sentiment > config.bullish_threshold
        and distinct_hosts(sources) < config.min_bullish_sources
    ):
        notes.append(
            f"{code}: sentimento {sentiment:.2f} rebaixado a {config.bullish_threshold:.2f} "
            f"(menos de {config.min_bullish_sources} fontes distintas)"
        )
        sentiment = config.bullish_threshold
    return AssetView(
        asset=code,
        sentiment=sentiment,
        confidence=confidence,
        horizon=draft.horizon,
        catalysts=_texts(draft.catalysts, config),
        risk_flags=_texts(draft.risk_flags, config),
        veto=draft.veto,
        rationale=_text(draft.rationale, config.max_text_chars),
        sources=sources[: config.max_list_items],
    )


def apply_safety(
    draft: MarketViewDraft,
    *,
    allowed_assets: Collection[str],
    as_of: datetime,
    config: SafetyConfig,
) -> SafetyResult:
    """Converte a saída do LLM numa leitura saneada, registrando cada ajuste."""
    notes: list[str] = []
    allowed = {a.upper() for a in allowed_assets}
    views: dict[str, AssetView] = {}
    for asset in draft.assets:
        code = asset.asset.strip().upper()
        if code not in allowed:
            notes.append(f"{code or '?'}: fora do universo recebido, descartado")
            continue
        asset_view = _asset_view(asset, code, config, notes)
        if code in views:
            notes.append(f"{code}: leituras repetidas combinadas pelo lado conservador")
            asset_view = _merge(views[code], asset_view)
        views[code] = asset_view

    exposure = _clamp(draft.exposure_multiplier, 0.0, 1.0, 0.0)
    if exposure != draft.exposure_multiplier:
        notes.append(f"exposure_multiplier {draft.exposure_multiplier} truncado a {exposure}")
    global_sentiment = _clamp(draft.global_sentiment, -1.0, 1.0, 0.0)
    if global_sentiment != draft.global_sentiment:
        notes.append("global_sentiment truncado à faixa")
    view = MarketView(
        as_of=as_of,
        market_regime=draft.market_regime,
        global_sentiment=global_sentiment,
        exposure_multiplier=exposure,
        global_risk_flags=_texts(draft.global_risk_flags, config),
        assets=tuple(views.values()),
    )
    return SafetyResult(view=view, adjustments=tuple(notes))
