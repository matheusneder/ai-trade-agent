"""Allocation profiles (``config/profiles.yaml``), validated by schema (doc 03, §8)."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trade_agent.execution.orders import EntryMode, ProtectionPolicy, StopMode, TakeProfitMode
from trade_agent.market.universe import Tier
from trade_agent.signals import SignalParams

Fraction = Annotated[Decimal, Field(ge=0, le=1)]
Percent = Annotated[Decimal, Field(gt=0, lt=100)]
Timeframe = Literal["15m", "1h", "4h", "1d"]


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
    trailing_delta_bps: int | None = Field(default=None, ge=10, le=2000)

    @model_validator(mode="after")
    def _check(self) -> "TakeProfitConfig":
        if self.mode is TakeProfitMode.TRAILING and self.trailing_delta_bps is None:
            raise ValueError("take-profit trailing exige trailing_delta_bps")
        return self


class ProtectionConfig(_Strict):
    stop: StopConfig
    take_profit: TakeProfitConfig
    break_even_after_r: float | None = Field(default=None, gt=0)
    max_holding: timedelta | None = None

    @field_validator("max_holding", mode="before")
    @classmethod
    def _parse_duration(cls, value: Any) -> Any:
        if isinstance(value, str) and value[-1:] in "hd" and value[:-1].isdigit():
            amount = int(value[:-1])
            return timedelta(hours=amount) if value.endswith("h") else timedelta(days=amount)
        return value

    def policy(self, stop_pct: Decimal) -> ProtectionPolicy:
        """Concrete policy; ``stop_pct`` comes from the signal (ATR), capped by ``max_pct``."""
        if self.stop.mode is StopMode.FIXED:
            return ProtectionPolicy(
                take_profit_mode=self.take_profit.mode,
                take_profit_pct=self.take_profit.activation_pct,
                take_profit_trailing_bips=self.take_profit.trailing_delta_bps,
                stop_mode=StopMode.FIXED,
                stop_pct=min(stop_pct, self.stop.max_pct or stop_pct),
            )
        return ProtectionPolicy(
            take_profit_mode=self.take_profit.mode,
            take_profit_pct=self.take_profit.activation_pct,
            take_profit_trailing_bips=self.take_profit.trailing_delta_bps,
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
    weight: Fraction = Decimal(0)
    min_confidence: Fraction = Decimal("0.5")
    on_failure: Literal["ta_only", "ta_only_reduced", "pause_entries"] = "ta_only_reduced"


# Signal parameters defined in ``protection.stop``: they cannot be adjusted in ``signals``.
STOP_SIGNAL_PARAMS = frozenset({"atr_stop_mult", "max_stop_pct"})


class ProfileConfig(_Strict):
    code: str = Field(pattern=r"^[a-z0-9]{1,12}$")
    """Short code used in the order IDs (``ta1-{code}-...``)."""
    enabled: bool = True
    capital_share: Fraction
    timeframe: Timeframe
    tiers: dict[Tier, Fraction]
    allocation: AllocationConfig
    entry: EntryConfig
    protection: ProtectionConfig
    exits: ExitConfig = ExitConfig()
    llm: LlmConfig = LlmConfig()
    signals: dict[str, float | bool] = Field(default_factory=dict)
    """Adjustments of the signal parameters (see ``SignalParams``)."""

    @field_validator("signals")
    @classmethod
    def _known_signal_params(cls, value: dict[str, float | bool]) -> dict[str, float | bool]:
        unknown = set(value) - set(SignalParams.__dataclass_fields__)
        if unknown:
            raise ValueError(f"parâmetros de sinal desconhecidos: {sorted(unknown)}")
        reserved = set(value) & STOP_SIGNAL_PARAMS
        if reserved:
            raise ValueError(f"defina {sorted(reserved)} em protection.stop")
        return value

    def signal_params(self) -> SignalParams:
        """The profile's signal parameters. With a fixed stop, the ATR multiple and the stop cap
        come from ``protection.stop`` (single source for the agent and the lab)."""
        overrides: dict[str, Any] = dict(self.signals)
        stop = self.protection.stop
        if stop.mode is StopMode.FIXED:
            overrides["atr_stop_mult"] = stop.atr_mult
            overrides["max_stop_pct"] = float(stop.max_pct or 0) / 100
        return SignalParams(**overrides)

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
