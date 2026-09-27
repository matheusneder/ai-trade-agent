"""Condições de parada (``config/stop_conditions.yaml``), validadas por schema."""

import re
from datetime import datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

_DURATION = re.compile(r"^(\d+)([mhd])$")
_WINDOW = re.compile(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})$")


class Action(StrEnum):
    PAUSE = "pause"
    """Sem novas entradas; proteções e saídas por regra continuam. Volta após o cooldown."""
    HALT = "halt"
    """Sem entradas nem saídas por regra; proteções mantidas. Volta só manualmente."""
    FLATTEN = "flatten"
    """Cancela as proteções, vende tudo e termina em ``HALT``."""


def parse_duration(value: Any) -> Any:
    if isinstance(value, str) and (match := _DURATION.match(value.strip())):
        amount, unit = int(match.group(1)), match.group(2)
        return {"m": timedelta(minutes=amount), "h": timedelta(hours=amount)}.get(
            unit, timedelta(days=amount)
        )
    return value


def parse_window(window: str) -> tuple[time, time]:
    match = _WINDOW.match(window)
    try:
        if match is None:
            raise ValueError
        h1, m1, h2, m2 = (int(g) for g in match.groups())
        return time(h1, m1), time(h2, m2)
    except ValueError:
        raise ValueError(f"janela inválida (use HH:MM-HH:MM): {window}") from None


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Trigger(_Strict):
    value: float | None = None
    action: Action
    cooldown: timedelta | None = None
    """Só para ``pause``: retorno automático após o prazo (sem prazo, só manual)."""

    _cooldown = field_validator("cooldown", mode="before")(parse_duration)


class GlobalConditions(_Strict):
    max_daily_loss_pct: Trigger | None = None
    max_drawdown_pct: Trigger | None = None
    max_consecutive_losses: Trigger | None = None
    btc_move_1h_pct: Trigger | None = None
    """Negativo: queda (move ≤ valor); positivo: alta (move ≥ valor)."""
    quote_depeg_pct: Trigger | None = None
    fear_greed_below: Trigger | None = None
    api_error_rate_5m: Trigger | None = None
    reconcile_mismatch: Trigger | None = None
    profit_target_pct: Trigger | None = None
    trading_window_utc: list[str] | None = None
    """Janelas ``HH:MM-HH:MM`` (UTC) em que novas entradas são permitidas."""

    @field_validator("trading_window_utc")
    @classmethod
    def _windows(cls, value: list[str] | None) -> list[str] | None:
        for window in value or []:
            parse_window(window)
        return value


class ProfileConditions(_Strict):
    max_daily_loss_pct: Trigger | None = None
    max_consecutive_losses: Trigger | None = None


class PreTradeConfig(_Strict):
    min_reward_risk: float = Field(default=1.5, gt=0)
    """Alvo (ativação do take-profit) ÷ stop, já descontadas as taxas de ida e volta."""
    round_trip_fee_pct: float = Field(default=0.2, ge=0)
    risk_tolerance_pct: float = Field(default=5, ge=0)
    """Folga sobre o risco por trade (arredondamentos de preço e quantidade)."""


class StopConditions(_Strict):
    global_: GlobalConditions = Field(default_factory=GlobalConditions, alias="global")
    per_profile: dict[str, ProfileConditions] = Field(default_factory=dict)
    pre_trade: PreTradeConfig = PreTradeConfig()


def in_trading_window(windows: list[str] | None, now: datetime) -> bool:
    """Sem janelas configuradas, sempre permitido. Janelas podem cruzar a meia-noite."""
    if not windows:
        return True
    current = now.time().replace(second=0, microsecond=0)
    for window in windows:
        start, end = parse_window(window)
        inside = start <= current <= end if start <= end else current >= start or current <= end
        if inside:
            return True
    return False


def load_stop_conditions(path: Path | str) -> StopConditions:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return StopConditions.model_validate(data)
