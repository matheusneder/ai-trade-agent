"""Market regime flips: recorded once per candle close, announced only when they change (D-043).

The alert names the timeframe's profiles that use the regime filter and the ones that do not
(D-044): BTC below its EMA 200 only stops the former."""

from pathlib import Path

from tests.support.local_profiles import local_config
from trade_agent.decision.engine import REGIME_EVENT, RegimeWatch, regime_text
from trade_agent.persistence.store import Severity, Store
from trade_agent.signals import Regime
from trade_agent.strategy.profiles import ProfileConfig, load_strategy_config

UP = Regime(up=True, close=101.0, ema_slow=100.0)
DOWN = Regime(up=False, close=98.0, ema_slow=100.0)
REPOSITORY = load_strategy_config(Path(__file__).parents[3] / "config" / "profiles.yaml")
GATED = "swing_trend, momentum_alpha, alt_atr"


def _watch(
    store: Store, alerts: list[str], profiles: dict[str, ProfileConfig] | None = None
) -> RegimeWatch:
    async def notify(severity: Severity, text: str) -> None:
        assert severity is Severity.INFO
        alerts.append(text)

    profiles = REPOSITORY.enabled_profiles() if profiles is None else profiles
    return RegimeWatch(store, notify, ema=200, profiles=profiles)


async def _flips(store: Store) -> list[dict[str, object]]:
    events = await store.recent_events(limit=100)
    return [dict(e.payload) for e in reversed(events) if e.kind == REGIME_EVENT]


async def test_first_reading_is_recorded_and_only_flips_are_announced(store: Store) -> None:
    alerts: list[str] = []
    watch = _watch(store, alerts)
    await watch.observe("4h", 1_000, DOWN)
    await watch.observe("4h", 1_000, UP)  # another profile of the same close: ignored
    await watch.observe("4h", 2_000, DOWN)  # no flip
    assert alerts == []
    (first,) = await _flips(store)
    assert (first["up"], first["previous"], first["timeframe"]) == (False, None, "4h")
    assert first["distance_pct"] == -2.0 and first["benchmark"] == "BTCUSDT"
    await watch.observe("4h", 3_000, UP)
    assert alerts == [
        "Regime de mercado (4h) virou para alta: BTCUSDT fechou em 101.00, acima da EMA200 "
        f"(100.00, +1.00%); os setups de compra dos perfis com o filtro ({GATED}) voltam a valer."
    ]
    await watch.observe("4h", 2_500, DOWN)  # an older candle (a late profile): ignored
    assert len(alerts) == 1
    flips = await _flips(store)
    assert [(f["up"], f["previous"]) for f in flips] == [(False, None), (True, False)]
    assert flips[1]["candle"] == "1970-01-01T00:00:03+00:00"


async def test_a_restart_does_not_announce_the_same_regime_again(store: Store) -> None:
    alerts: list[str] = []
    await _watch(store, alerts).observe("4h", 1_000, UP)
    restarted = _watch(store, alerts)
    await restarted.observe("4h", 1_000, UP)
    await restarted.observe("4h", 2_000, UP)
    assert alerts == [] and len(await _flips(store)) == 1
    await restarted.observe("4h", 3_000, DOWN)
    assert len(alerts) == 1 and alerts[0].startswith("Regime de mercado (4h) virou para baixa")
    assert (
        f"os perfis com o filtro de regime ({GATED}) não abrem compras até o BTCUSDT fechar "
        "acima da EMA200." in alerts[0]
    )
    assert await store.get_checkpoint("market.regime.4h") == {"up": False, "candle_ms": 3_000}


async def test_each_timeframe_has_its_own_regime(store: Store) -> None:
    alerts: list[str] = []
    watch = _watch(store, alerts)
    await watch.observe("4h", 1_000, UP)
    await watch.observe("1h", 1_000, DOWN)  # its first reading, not a flip of the 4h one
    assert alerts == []
    assert [f["timeframe"] for f in await _flips(store)] == ["4h", "1h"]


def test_the_alert_tells_the_profiles_with_the_filter_from_the_ones_without_it() -> None:
    config = local_config(local={"timeframe": "4h"})
    legacy = config.profiles["conservador"]
    no_filter = legacy.model_copy(update={"signals": {"use_regime_filter": False}})
    profiles = {"conservador": legacy, "sem_filtro": no_filter, "local": config.profiles["local"]}
    text = RegimeWatch(None, None, ema=200, profiles=profiles).text("4h", DOWN)  # type: ignore[arg-type]
    assert text.endswith(
        "o perfil com o filtro de regime (conservador) não abre compras até o BTCUSDT fechar "
        "acima da EMA200; sem_filtro, local seguem os sinais do próprio par."
    )
    alone = regime_text("4h", UP, ema=200, gated=[], free=["local"])
    assert alone.endswith(
        "; nenhum perfil deste timeframe usa o filtro de regime; local segue os sinais do "
        "próprio par."
    )
    other_timeframe = RegimeWatch(None, None, ema=200, profiles=profiles).text("1h", DOWN)  # type: ignore[arg-type]
    assert other_timeframe.endswith("nenhum perfil deste timeframe usa o filtro de regime.")
