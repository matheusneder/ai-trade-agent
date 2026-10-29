"""Allocation profiles (``config/profiles.yaml``), validated by schema (doc 03, §8)."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trade_agent.execution.orders import EntryMode, ProtectionPolicy, StopMode, TakeProfitMode
from trade_agent.market.universe import Tier
from trade_agent.risk.scope import ScopeLimits
from trade_agent.signals import Family, LocalParams, SignalParams
from trade_agent.signals.local import PARAMS as LOCAL_PARAMS
from trade_agent.signals.local import STOP_PARAMS as LOCAL_STOP_PARAMS

Fraction = Annotated[Decimal, Field(ge=0, le=1)]
Percent = Annotated[Decimal, Field(gt=0, lt=100)]
Timeframe = Literal["15m", "1h", "4h", "1d"]
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
    max_holding: timedelta | None = None

    _max_holding = field_validator("max_holding", mode="before")(parse_duration)

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
