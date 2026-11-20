"""Allocation profiles (``config/profiles.yaml``), validated by schema (doc 03, §8)."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trade_agent.execution.orders import EntryMode, ProtectionPolicy, StopMode, TakeProfitMode
from trade_agent.market.candles import INTERVAL_MS
from trade_agent.market.universe import Tier
from trade_agent.risk.conditions import parse_duration as parse_interval
from trade_agent.risk.scope import ScopeLimits
from trade_agent.signals import Family, LocalParams, SignalParams
from trade_agent.signals.local import PARAMS as LOCAL_PARAMS
from trade_agent.signals.local import STOP_PARAMS as LOCAL_STOP_PARAMS

Fraction = Annotated[Decimal, Field(ge=0, le=1)]
Percent = Annotated[Decimal, Field(gt=0, lt=100)]
Timeframe = Literal["15m", "1h", "4h", "1d"]
MIN_CYCLE = timedelta(minutes=5)
"""Shortest decision ``cycle`` (D-053): the universe is cached for 5 minutes, and the lead
traders are read every 5 minutes at most (``LeadsConfig.poll_interval``)."""
MIN_TRAILING_BIPS, MAX_TRAILING_BIPS = 10, 2000
"""Binance's ``TRAILING_DELTA`` range for the ``trailingDelta`` of an ATR-based trailing."""


def parse_duration(value: Any) -> Any:
    """``"12h"`` / ``"3d"`` → ``timedelta``; anything else goes to pydantic."""
    if isinstance(value, str) and value[-1:] in "hd" and value[:-1].isdigit():
        amount = int(value[:-1])
        return timedelta(hours=amount) if value.endswith("h") else timedelta(days=amount)
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AccountConfig(_Strict):
    quote_asset: str = "USDT"
    managed_capital: Decimal = Field(gt=0)
    """Capital cap (quote asset) the agent may use; the rest of the account is ignored."""
    one_position_per_asset: bool = True


class AllocationConfig(_Strict):
    max_open_positions: int = Field(ge=1, le=50)
    max_position_pct: Fraction
    cash_reserve_pct: Fraction
    risk_per_trade_pct: Percent
    """Percentage of the profile's capital lost if the stop fills."""


class EntryConfig(_Strict):
    min_score: float = Field(ge=-1, le=1)
    order: EntryMode = EntryMode.LIMIT_FOK
    max_slippage_bps: int = Field(default=20, ge=0, le=500)


class StopConfig(_Strict):
    mode: StopMode
    atr_mult: float | None = Field(default=None, gt=0)
    max_pct: Percent | None = None
    trailing_delta_bps: int | None = Field(default=None, ge=10, le=2000)

    @model_validator(mode="after")
    def _check(self) -> "StopConfig":
        if self.mode is StopMode.FIXED and (self.atr_mult is None or self.max_pct is None):
            raise ValueError("stop fixo exige atr_mult e max_pct")
        if self.mode is StopMode.TRAILING and self.trailing_delta_bps is None:
            raise ValueError("stop trailing exige trailing_delta_bps")
        return self


class TakeProfitConfig(_Strict):
    mode: TakeProfitMode
    activation_pct: Percent
    """Fixed activation; with ``activation_atr_mult``, ``activation_r`` or ``signal_target``,
    the floor of the variable one."""
    trailing_delta_bps: int | None = Field(default=None, ge=10, le=2000)
    activation_atr_mult: float | None = Field(default=None, gt=0)
    """Optional: activation at this multiple of the entry's ATR%, clipped to
    [``activation_pct``, ``activation_max_pct``]."""
    activation_max_pct: Percent | None = None
    activation_r: float | None = Field(default=None, gt=0)
    """Pair-local families (D-044): activation (or target) at this multiple of the risk, net
    of costs: ``r × (stop + cost) + cost``, with the profile's ``risk.cost_pct``."""
    trailing_atr_mult: float | None = Field(default=None, gt=0)
    """Pair-local families: ``trailingDelta`` at this multiple of the entry's ATR%."""
    signal_target: bool = False
    """Pair-local families: ``limit`` target from the signal (``Signal.target_pct``)."""

    @model_validator(mode="after")
    def _check(self) -> "TakeProfitConfig":
        trailing = (self.trailing_delta_bps is not None) + (self.trailing_atr_mult is not None)
        if self.mode is TakeProfitMode.TRAILING and trailing != 1:
            raise ValueError("take-profit trailing exige trailing_delta_bps ou trailing_atr_mult")
        if self.mode is TakeProfitMode.LIMIT and trailing:
            raise ValueError("take-profit limit não usa trailing")
        if (self.activation_atr_mult is None) != (self.activation_max_pct is None):
            raise ValueError("ativação por ATR exige activation_atr_mult e activation_max_pct")
        if self.activation_max_pct is not None and self.activation_max_pct < self.activation_pct:
            raise ValueError("activation_max_pct deve ser >= activation_pct")
        variable = (
            (self.activation_atr_mult is not None)
            + (self.activation_r is not None)
            + self.signal_target
        )
        if variable > 1:
            raise ValueError("use só um de activation_atr_mult, activation_r e signal_target")
        if self.signal_target and self.mode is not TakeProfitMode.LIMIT:
            raise ValueError("signal_target exige take-profit limit")
        return self

    @property
    def local_only(self) -> bool:
        """Uses an option of the pair-local families."""
        r_or_atr = self.activation_r is not None or self.trailing_atr_mult is not None
        return r_or_atr or self.signal_target

    def trailing_bips_for(self, atr_pct: Decimal | None) -> int | None:
        """``trailingDelta`` (bips) for an entry with ``atr_pct`` (percentage): the fixed one,
        or the ATR-based one rounded and clipped to Binance's range."""
        if self.trailing_atr_mult is None:
            return self.trailing_delta_bps
        bips = round(Decimal(str(self.trailing_atr_mult)) * (atr_pct or Decimal(0)) * 100)
        return min(max(int(bips), MIN_TRAILING_BIPS), MAX_TRAILING_BIPS)

    def activation_for(self, atr_pct: Decimal | None) -> Decimal:
        """Activation (percentage) for an entry with ``atr_pct`` (percentage); the fixed one
        when the ATR mode is off or the ATR is unknown."""
        if self.activation_atr_mult is None or self.activation_max_pct is None or not atr_pct:
            return self.activation_pct
        activation = Decimal(str(self.activation_atr_mult)) * atr_pct
        return min(max(activation, self.activation_pct), self.activation_max_pct)


class ProtectionConfig(_Strict):
    stop: StopConfig
    take_profit: TakeProfitConfig
    break_even_after_r: float | None = Field(default=None, gt=0)
    break_even_intrabar: bool = False
    """Check ``break_even_after_r`` between the candle closes too, on the bid at the risk
    check's cadence (D-047); the lab checks it on each detail candle. Off: only at the close."""
    max_holding: timedelta | None = None

    _max_holding = field_validator("max_holding", mode="before")(parse_duration)

    @model_validator(mode="after")
    def _break_even(self) -> "ProtectionConfig":
        if self.break_even_intrabar and self.break_even_after_r is None:
            raise ValueError("break_even_intrabar exige break_even_after_r")
        return self

    def policy(
        self,
        stop_pct: Decimal,
        atr_pct: Decimal | None = None,
        *,
        cost_pct: Decimal = Decimal(0),
        target_pct: Decimal | None = None,
    ) -> ProtectionPolicy:
        """Concrete policy; ``stop_pct`` comes from the signal (ATR), capped by ``max_pct``;
        ``atr_pct`` (percentage) sets the take-profit activation in the ATR mode. The
        pair-local options (D-044) also read the round-trip ``cost_pct`` (``activation_r``)
        and the signal's ``target_pct`` (``signal_target``), both percentages."""
        take_profit = self.take_profit
        activation = take_profit.activation_for(atr_pct)
        if take_profit.activation_r is not None:
            capped = min(stop_pct, self.stop.max_pct or stop_pct)
            net = Decimal(str(take_profit.activation_r)) * (capped + cost_pct) + cost_pct
            activation = max(activation, net)
        if take_profit.signal_target:
            activation = max(activation, target_pct or Decimal(0))
        trailing = take_profit.trailing_bips_for(atr_pct)
        if self.stop.mode is StopMode.FIXED:
            return ProtectionPolicy(
                take_profit_mode=take_profit.mode,
                take_profit_pct=activation,
                take_profit_trailing_bips=trailing,
                stop_mode=StopMode.FIXED,
                stop_pct=min(stop_pct, self.stop.max_pct or stop_pct),
            )
        return ProtectionPolicy(
            take_profit_mode=take_profit.mode,
            take_profit_pct=activation,
            take_profit_trailing_bips=trailing,
            stop_mode=StopMode.TRAILING,
            stop_trailing_bips=self.stop.trailing_delta_bps,
        )

    def stop_distance_pct(self, signal_stop_pct: Decimal) -> Decimal:
        """Stop distance used for sizing (percentage)."""
        if self.stop.mode is StopMode.FIXED:
            return min(signal_stop_pct, self.stop.max_pct or signal_stop_pct)
        return Decimal(self.stop.trailing_delta_bps or 0) / 100


class ExitConfig(_Strict):
    exit_score: float = Field(default=-0.2, ge=-1, le=1)
    exit_after_cycles: int = Field(default=2, ge=1)


class LlmConfig(_Strict):
    enabled: bool = True
    """False is a pure TA baseline: no research, opinions, vetoes or degradation."""
    weight: Fraction = Decimal(0)
    min_confidence: Fraction = Decimal("0.5")
    on_failure: Literal["ta_only", "ta_only_reduced", "pause_entries"] = "ta_only_reduced"


class ScopeRiskConfig(_Strict):
    """Risk budget of a pair-local profile (D-044), on top of the global conditions.

    The production Risk Guard (``RiskGuard.track_scopes``) and the lab (``lab/freqtrade``,
    ``lab.paper``) run the same pauses, :mod:`trade_agent.risk.scope`. A pause only stops new
    entries; the native protection and the technical exits go on.
    """

    cost_pct: Annotated[Decimal, Field(ge=0, lt=100)]
    """Adverse round-trip cost (fees, spread and slippage, %) in the sizing and in
    ``activation_r``: ``notional ≤ capital × risk ÷ (stop + cost)``."""
    max_open_risk_pct: Percent | None = None
    """Sum of the open positions' initial risk, % of the profile's capital."""
    daily_loss_pct: Percent | None = None
    """Marked-to-market loss since 00:00 UTC (% of the profile's capital) that pauses it."""
    consecutive_losses: int | None = Field(default=None, ge=1)
    cooldown: timedelta = timedelta(hours=24)
    """Pause after ``daily_loss_pct`` or ``consecutive_losses``."""
    max_drawdown_pct: Percent | None = None
    """Marked-to-market drawdown from the peak that pauses new entries until a review (no
    expiry); unlike ``halt``, the technical exits go on."""
    reentry_candles: int = Field(default=0, ge=0)
    """Candles without a new entry on a pair after a stop on it."""

    _cooldown = field_validator("cooldown", mode="before")(parse_duration)

    def limits(self, capital: Decimal, candle: timedelta) -> ScopeLimits:
        """The scope's limits on ``capital``, with the re-entry wait in ``candle``s."""

        def pct(value: Decimal | None) -> float | None:
            return None if value is None else float(value)

        return ScopeLimits(
            capital=float(capital),
            daily_loss_pct=pct(self.daily_loss_pct),
            consecutive_losses=self.consecutive_losses,
            cooldown=self.cooldown,
            max_drawdown_pct=pct(self.max_drawdown_pct),
            reentry=candle * self.reentry_candles,
        )


class LeadSelectionConfig(_Strict):
    """Which lead traders of Binance's Spot Copy Trading ranking a profile follows (D-052).

    The figures are the ranking's own, over its 90-day window. No criterion is the cumulative
    return alone: a portfolio must also be old enough, have its owner's money in it, a bounded
    drawdown, a risk-adjusted return, copiers that did not lose much, gains spread over the
    weeks, a recent gain and a human trading rhythm (:mod:`trade_agent.leads.ranking`).

    A test profile may leave the performance criteria out (``null``) and follow the traders
    that trade most often instead (``rank_by: activity``, D-054).
    """

    rank_by: Literal["score", "activity"] = "score"
    """Which eligible traders are followed: the best scores, or the ones that trade most often
    whatever their results (trades per day, D-054)."""
    pool_pages: int = Field(default=5, ge=1, le=20)
    """Pages of 30 portfolios read from the ranking, best Sharpe ratio first."""
    shortlist: int = Field(default=20, ge=1, le=60)
    """Portfolios that passed the ranking figures whose PnL curve and trades are read (two
    requests each), in the ranking's order."""
    max_leads: int = Field(default=6, ge=1, le=20)
    """Lead traders followed at the same time."""
    min_days: int = Field(default=90, ge=1)
    """Days the portfolio has been trading."""
    min_balance: Decimal = Field(default=Decimal(1000), ge=0)
    """The lead trader's own money in the portfolio (its AUM minus the copiers'), in USDT."""
    min_roi_pct: float | None = 3.0
    """``null`` here and in the other performance criteria: not checked (D-054)."""
    max_drawdown_pct: float | None = Field(default=20.0, gt=0)
    min_sharpe: float | None = 1.0
    """A portfolio without a Sharpe ratio (Binance gives none to the young ones) is refused."""
    max_copier_loss_pct: float | None = Field(default=5.0, ge=0)
    """What the copiers lost over the window, in % of the money they have in the portfolio,
    above which it is refused whatever its own ROI."""
    min_positive_weeks_pct: float | None = Field(default=55.0, ge=0, le=100)
    """Share of the 7-day steps of the cumulative PnL curve that ended higher."""
    min_recent_gain_pct: float | None = 0.0
    """PnL of the last 30 days, in % of the trader's own balance."""
    max_idle: timedelta = timedelta(days=7)
    """Longest time since the portfolio's last trade."""
    min_trades_30d: int = Field(default=4, ge=0)
    max_trades_per_day: float = Field(default=20.0, gt=0)
    """Above it the portfolio is taken for a grid bot, whose buys are no directional signal."""

    _max_idle = field_validator("max_idle", mode="before")(parse_interval)

    @model_validator(mode="after")
    def _check(self) -> "LeadSelectionConfig":
        if self.shortlist < self.max_leads:
            raise ValueError("shortlist deve ser >= max_leads")
        return self


class LeadConsensusConfig(_Strict):
    """When the followed lead traders' trades are a signal (D-052)."""

    min_backers: int = Field(default=2, ge=1)
    """Lead traders that bought the asset, net of the ones that sold it: one trader alone
    never opens a position with the default. The same margin of sellers is the exit."""
    window: timedelta = timedelta(hours=24)
    """How far back a lead trader's trades count."""
    min_buy_pct: Percent = Decimal(2)
    """Net purchase (or sale) of the asset in the window, in % of the trader's own balance,
    for it to count."""
    max_symbols_per_lead: int = Field(default=3, ge=1)
    """A trader backs at most this many assets at a time (its largest net purchases), so one
    that buys everything does not weigh on every consensus."""
    max_premium_pct: Percent = Decimal("1.5")
    """No entry once the ask is this far above the average price the backers paid."""

    _window = field_validator("window", mode="before")(parse_interval)


class LeadsConfig(_Strict):
    """The lead traders a ``lead_consensus`` profile follows (D-052): who, when their trades
    are a signal, and how the public source is read (:mod:`trade_agent.leads`)."""

    selection: LeadSelectionConfig = LeadSelectionConfig()
    consensus: LeadConsensusConfig = LeadConsensusConfig()
    poll_interval: timedelta = timedelta(minutes=15)
    """How often the followed traders' trade history is read."""
    ranking_interval: timedelta = timedelta(hours=6)
    """How often the ranking is read again and the traders are chosen again."""
    max_data_age: timedelta = timedelta(minutes=45)
    """A trader whose history was last read longer ago than this does not count."""
    max_ranking_age: timedelta = timedelta(hours=24)
    """Without a new ranking for this long, nobody is followed and no entry goes out."""
    backoff: timedelta = timedelta(hours=1)
    """Pause of every request after the source refuses one (HTTP 403, 418, 429 or 451)."""
    request_delay_s: float = Field(default=0.5, ge=0, le=10)
    """Wait between two requests of the same reading."""

    _durations = field_validator(
        "poll_interval", "ranking_interval", "max_data_age", "max_ranking_age", "backoff",
        mode="before",
    )(parse_interval)  # fmt: skip

    @model_validator(mode="after")
    def _check(self) -> "LeadsConfig":
        if self.poll_interval < timedelta(minutes=5):
            raise ValueError("poll_interval deve ser >= 5m")
        if self.max_data_age < self.poll_interval:
            raise ValueError("max_data_age deve ser >= poll_interval")
        if self.max_ranking_age < self.ranking_interval:
            raise ValueError("max_ranking_age deve ser >= ranking_interval")
        return self


# Signal parameters defined in ``protection.stop``: they cannot be adjusted in ``signals``.
STOP_SIGNAL_PARAMS = frozenset({"atr_stop_mult", "max_stop_pct"})


class ProfileConfig(_Strict):
    code: str = Field(pattern=r"^[a-z0-9]{1,12}$")
    """Short code used in the order IDs (``ta1-{code}-...``)."""
    enabled: bool = True
    family: Family = Family.LEGACY
    """Strategy family (D-044): ``legacy`` is the score and setups of ``SignalParams``; the
    others decide on the pair's own candles (``trade_agent.signals.local``)."""
    capital_share: Fraction
    timeframe: Timeframe
    cycle: timedelta | None = None
    """How often a ``lead_consensus`` profile decides, on the last closed candle of its
    ``timeframe`` (D-053): its consensus changes between the closes. ``None``, or the
    timeframe itself: at each close."""
    tiers: dict[Tier, Fraction]
    allocation: AllocationConfig
    entry: EntryConfig
    protection: ProtectionConfig
    exits: ExitConfig = ExitConfig()
    llm: LlmConfig = LlmConfig()
    signals: dict[str, float | bool] = Field(default_factory=dict)
    """Adjustments of the signal parameters (``SignalParams``, or the family's)."""
    risk: ScopeRiskConfig | None = None
    """Risk budget of a pair-local profile (D-044)."""
    leads: LeadsConfig | None = None
    """The lead traders a ``lead_consensus`` profile follows (D-052)."""

    _cycle = field_validator("cycle", mode="before")(parse_interval)

    @model_validator(mode="after")
    def _lead_options(self) -> "ProfileConfig":
        if self.family.needs_leads and self.leads is None:
            raise ValueError(f"família {self.family.value} exige leads")
        if self.leads is not None and not self.family.needs_leads:
            raise ValueError("leads é só da família lead_consensus")
        if self.cycle is not None:
            # the other families' signals change only at the candle close (D-051 holds)
            if not self.family.needs_leads:
                raise ValueError("cycle é só da família lead_consensus")
            candle = timedelta(milliseconds=INTERVAL_MS[self.timeframe])
            minute = timedelta(minutes=1)
            if not MIN_CYCLE <= self.cycle <= candle or candle % self.cycle or self.cycle % minute:
                raise ValueError(
                    f"cycle deve ter minutos inteiros, ser >= 5m e dividir o timeframe "
                    f"({self.timeframe})"
                )
        return self

    @model_validator(mode="after")
    def _known_signal_params(self) -> "ProfileConfig":
        legacy = self.family is Family.LEGACY
        kind = SignalParams if legacy else LOCAL_PARAMS[self.family]
        unknown = set(self.signals) - set(kind.__dataclass_fields__)
        if unknown:
            raise ValueError(f"parâmetros de sinal desconhecidos: {sorted(unknown)}")
        reserved = set(self.signals) & (STOP_SIGNAL_PARAMS if legacy else LOCAL_STOP_PARAMS)
        if reserved:
            raise ValueError(f"defina {sorted(reserved)} em protection.stop")
        return self

    @model_validator(mode="after")
    def _family_options(self) -> "ProfileConfig":
        protection = self.protection
        if self.family is Family.LEGACY:
            if self.risk is not None or protection.take_profit.local_only:
                raise ValueError("risk e o alvo por R, ATR ou sinal são só das famílias locais")
            return self
        problems = []
        if self.llm.enabled:
            problems.append("llm.enabled false")
        if protection.stop.mode is not StopMode.FIXED:
            problems.append("stop fixo")
        if protection.break_even_after_r is not None:
            problems.append("sem break_even_after_r")
        if "exits" in self.model_fields_set:
            problems.append("sem exits (a saída técnica é a da família)")
        if self.risk is None:
            problems.append("risk")
        if problems:
            raise ValueError(f"família {self.family.value} exige: {', '.join(problems)}")
        return self

    def signal_params(self) -> SignalParams:
        """The legacy family's signal parameters. With a fixed stop, the ATR multiple and the
        stop cap come from ``protection.stop`` (single source for the agent and the lab)."""
        if self.family.local:
            raise ValueError(f"família {self.family.value}: use local_params()")
        overrides: dict[str, Any] = dict(self.signals)
        stop = self.protection.stop
        if stop.mode is StopMode.FIXED:
            overrides["atr_stop_mult"] = stop.atr_mult
            overrides["max_stop_pct"] = float(stop.max_pct or 0) / 100
        return SignalParams(**overrides)

    def local_params(self) -> LocalParams:
        """A pair-local family's parameters (D-044): ``protection.stop`` gives the stop's
        minimum distance in ATRs (``atr_mult``) and its cap (``max_pct``)."""
        if not self.family.local:
            raise ValueError("família legacy: use signal_params()")
        stop = self.protection.stop
        overrides: dict[str, Any] = dict(self.signals)
        overrides["min_stop_atr"] = stop.atr_mult
        overrides["max_stop_pct"] = float(stop.max_pct or 0) / 100
        return LOCAL_PARAMS[self.family](**overrides)

    @property
    def cadence(self) -> str:
        """When the profile decides, for the scheduler (:mod:`trade_agent.decision.schedule`):
        at each close of its ``timeframe``, or every ``cycle`` (``"10m"``, D-053)."""
        if self.cycle is None:
            return self.timeframe
        return f"{self.cycle // timedelta(minutes=1)}m"

    @property
    def uses_regime_filter(self) -> bool:
        """The setups wait for the benchmark above its slow EMA (D-016); never in the
        pair-local families (D-044)."""
        return not self.family.local and self.signal_params().use_regime_filter

    def cost_pct(self, default: Decimal = Decimal(0)) -> Decimal:
        """Round-trip cost (%) in the sizing: the profile's own (D-044) or ``default``."""
        return self.risk.cost_pct if self.risk is not None else default

    def tier_limit(self, tier: Tier) -> Decimal:
        return self.tiers.get(tier, Decimal(0))


class StrategyConfig(_Strict):
    account: AccountConfig
    profiles: dict[str, ProfileConfig]

    @model_validator(mode="after")
    def _check(self) -> "StrategyConfig":
        enabled = [p for p in self.profiles.values() if p.enabled]
        share = sum((p.capital_share for p in enabled), Decimal(0))
        if share > 1:
            raise ValueError(f"soma de capital_share dos perfis ativos > 1 ({share})")
        codes = [p.code for p in self.profiles.values()]
        if len(codes) != len(set(codes)):
            raise ValueError("códigos de perfil repetidos")
        return self

    def enabled_profiles(self) -> dict[str, ProfileConfig]:
        return {name: p for name, p in self.profiles.items() if p.enabled}

    def profile_capital(self, name: str) -> Decimal:
        return self.account.managed_capital * self.profiles[name].capital_share


def load_strategy_config(path: Path) -> StrategyConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return StrategyConfig.model_validate(data)
