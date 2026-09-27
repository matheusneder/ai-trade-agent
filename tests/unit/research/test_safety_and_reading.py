import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from trade_agent.research.config import SafetyConfig
from trade_agent.research.models import (
    AssetDraft,
    AssetView,
    Horizon,
    MarketRegime,
    MarketView,
    MarketViewDraft,
    NewsItem,
)
from trade_agent.research.reading import FAILURE_EXPOSURE, market_reading
from trade_agent.research.safety import SafetyResult, apply_safety, distinct_hosts
from trade_agent.strategy.profiles import LlmConfig

NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)
SAFETY = SafetyConfig(max_text_chars=40, max_list_items=2)


def _asset(**overrides: Any) -> AssetDraft:
    data: dict[str, Any] = {
        "asset": "SOL",
        "sentiment": 0.3,
        "confidence": 0.6,
        "horizon": Horizon.DAYS,
        "catalysts": [],
        "risk_flags": [],
        "veto": False,
        "rationale": "ok",
        "sources": [],
    }
    data.update(overrides)
    return AssetDraft(**data)


def _draft(*assets: AssetDraft, **overrides: Any) -> MarketViewDraft:
    data: dict[str, Any] = {
        "market_regime": MarketRegime.NEUTRAL,
        "global_sentiment": 0.1,
        "exposure_multiplier": 0.8,
        "global_risk_flags": [],
        "assets": list(assets),
    }
    data.update(overrides)
    return MarketViewDraft(**data)


def _safe(draft: MarketViewDraft, allowed: set[str] | None = None) -> SafetyResult:
    return apply_safety(draft, allowed_assets=allowed or {"sol", "BTC"}, as_of=NOW, config=SAFETY)


def test_news_dedupe_key_uses_url_or_normalized_title() -> None:
    a = NewsItem("rss", "Título  X", NOW, url="https://a.example/1")
    assert a.dedupe_key == NewsItem("rss", "outro", NOW, url="https://a.example/1").dedupe_key
    assert a.dedupe_key != NewsItem("x", "Título X", NOW, url="https://a.example/1").dedupe_key
    no_url = NewsItem("rss", "Título  X", NOW)
    assert no_url.dedupe_key == NewsItem("rss", "título x", NOW).dedupe_key
    assert len(no_url.dedupe_key) == 32


def test_clean_view_passes_unchanged() -> None:
    result = _safe(_draft(_asset(asset=" sol ")))
    assert result.adjustments == ()
    view = result.view
    assert view.as_of == NOW
    assert view.asset("SOL") == AssetView(
        asset="SOL", sentiment=0.3, confidence=0.6, horizon=Horizon.DAYS, rationale="ok"
    )
    assert view.asset("BTC") is None


def test_ranges_are_clamped_and_non_finite_become_conservative() -> None:
    draft = _draft(
        _asset(sentiment=-3.0, confidence=1.7),
        _asset(asset="BTC", sentiment=math.nan, confidence=math.inf),
        exposure_multiplier=1.4,
        global_sentiment=-2.0,
    )
    result = _safe(draft)
    view = result.view
    sol = view.asset("SOL")
    assert sol is not None and (sol.sentiment, sol.confidence) == (-1.0, 1.0)
    btc = view.asset("BTC")
    assert btc is not None and (btc.sentiment, btc.confidence) == (0.0, 0.0)
    assert view.exposure_multiplier == 1.0
    assert view.global_sentiment == -1.0
    assert len(result.adjustments) == 4
    nan_exposure = _safe(_draft(exposure_multiplier=math.nan)).view
    assert nan_exposure.exposure_multiplier == 0.0


def test_assets_outside_universe_are_dropped() -> None:
    result = _safe(_draft(_asset(asset="SCAM", veto=True), _asset(asset=" ")))
    assert result.view.assets == ()
    assert result.adjustments == (
        "SCAM: fora do universo recebido, descartado",
        "?: fora do universo recebido, descartado",
    )


def test_bullish_sentiment_requires_distinct_sources() -> None:
    same_site = ["https://www.a.example/1", "https://a.example/2", "not-a-url", "ftp://x.example"]
    weak = _safe(_draft(_asset(sentiment=0.9, sources=same_site)))
    reading = weak.view.asset("SOL")
    assert reading is not None
    assert reading.sentiment == 0.5
    assert reading.sources == ("https://www.a.example/1", "https://a.example/2")
    assert "rebaixado" in weak.adjustments[0]
    strong = _safe(
        _draft(_asset(sentiment=0.9, sources=["https://a.example/1", "https://b.example/2"]))
    )
    strong_sol = strong.view.asset("SOL")
    assert strong_sol is not None and strong_sol.sentiment == 0.9
    veto = _safe(_draft(_asset(sentiment=-0.9, veto=True))).view.asset("SOL")
    assert veto is not None and veto.veto  # veto vale sem fonte
    assert distinct_hosts(["https://www.x.example/a", "https://x.example/b", "mailto:x"]) == 1


def test_texts_and_lists_are_limited() -> None:
    long = "palavra " * 20
    draft = _draft(
        _asset(catalysts=["a", "a", "b", "c", " "], risk_flags=[long], rationale=long),
        global_risk_flags=["x", "y", "z"],
    )
    view = _safe(draft).view
    sol = view.asset("SOL")
    assert sol is not None
    assert sol.catalysts == ("a", "b")
    assert len(sol.risk_flags[0]) == SAFETY.max_text_chars
    assert sol.rationale.endswith("…")
    assert view.global_risk_flags == ("x", "y")


def test_repeated_assets_are_merged_conservatively() -> None:
    result = _safe(
        _draft(
            _asset(sentiment=0.4, confidence=0.9, risk_flags=["a"], sources=["https://a.example"]),
            _asset(sentiment=0.1, confidence=0.5, veto=True, risk_flags=["b"]),
        )
    )
    sol = result.view.asset("SOL")
    assert sol is not None
    assert (sol.sentiment, sol.confidence, sol.veto) == (0.1, 0.5, True)
    assert sol.risk_flags == ("a", "b")
    assert sol.sources == ("https://a.example",)
    assert "combinadas" in result.adjustments[0]


def _view(**overrides: Any) -> MarketView:
    data: dict[str, Any] = {
        "as_of": NOW,
        "market_regime": MarketRegime.RISK_OFF,
        "global_sentiment": -0.3,
        "exposure_multiplier": 0.4,
        "assets": (
            AssetView(asset="SOL", sentiment=-0.5, confidence=0.8, horizon=Horizon.DAYS, veto=True),
        ),
    }
    data.update(overrides)
    return MarketView(**data)


@pytest.mark.parametrize("on_failure", ["ta_only", "ta_only_reduced", "pause_entries"])
def test_reading_degrades_without_view_or_when_stale(on_failure: str) -> None:
    llm = LlmConfig.model_validate({"on_failure": on_failure})
    missing = market_reading(None, llm, now=NOW, max_age=timedelta(hours=8))
    assert missing.degraded and missing.reason == "sem leitura"
    assert missing.exposure_multiplier == FAILURE_EXPOSURE[on_failure]
    assert missing.opinions == {}
    stale = market_reading(
        _view(as_of=NOW - timedelta(hours=9)), llm, now=NOW, max_age=timedelta(hours=8)
    )
    assert stale.degraded and stale.reason == "leitura vencida"


def test_reading_maps_view_to_opinions() -> None:
    reading = market_reading(_view(), LlmConfig(), now=NOW, max_age=timedelta(hours=8))
    assert not reading.degraded
    assert reading.exposure_multiplier == Decimal("0.4")
    opinion = reading.opinions["SOL"]
    assert (opinion.sentiment, opinion.confidence, opinion.veto) == (
        Decimal("-0.5"),
        Decimal("0.8"),
        True,
    )
