"""Estados operacionais (doc 03, §10.2), globais e por perfil, persistidos no banco.

| Estado        | Novas entradas | Proteções | Saídas por regra | Retorno                    |
|---------------|----------------|-----------|------------------|----------------------------|
| ``RUNNING``   | sim            | sim       | sim              | —                          |
| ``PAUSED``    | não            | sim       | sim              | após o cooldown ou manual  |
| ``FLATTENING``| não            | vende     | —                | vai para ``HALTED``        |
| ``HALTED``    | não            | mantidas  | não              | só manual (``/resume``)    |

Gatilhos automáticos só **escalam** (nunca aliviam um estado mais restritivo); o retorno
a ``RUNNING`` é manual, exceto a pausa com cooldown vencido. Cada gatilho age uma vez por
ocorrência (``Fired``): enquanto a condição continua, o ``/resume`` e o fim do cooldown valem.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from trade_agent.persistence.store import Store

GLOBAL = "global"
FIRED_KEY = "risk.fired"


class OpState(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    HALTED = "halted"
    FLATTENING = "flattening"

    @property
    def rank(self) -> int:
        return list(OpState).index(self)

    @property
    def allows_entries(self) -> bool:
        return self is OpState.RUNNING

    @property
    def allows_rule_exits(self) -> bool:
        return self in {OpState.RUNNING, OpState.PAUSED}


@dataclass(frozen=True, slots=True)
class ScopeState:
    state: OpState = OpState.RUNNING
    reason: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    """Fim do cooldown de uma pausa (``None``: só manual)."""
    flattened: bool = False
    """``HALTED`` resultante de um flatten: o mesmo gatilho não o repete."""

    def current(self, now: datetime) -> "ScopeState":
        """Pausa com cooldown vencido volta a ``RUNNING``."""
        if self.state is OpState.PAUSED and self.until is not None and now >= self.until:
            return ScopeState()
        return self

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "reason": self.reason,
            "since": self.since.isoformat() if self.since else None,
            "until": self.until.isoformat() if self.until else None,
            "flattened": self.flattened,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ScopeState":
        return cls(
            state=OpState(data["state"]),
            reason=data.get("reason"),
            since=datetime.fromisoformat(data["since"]) if data.get("since") else None,
            until=datetime.fromisoformat(data["until"]) if data.get("until") else None,
            flattened=bool(data.get("flattened", False)),
        )


@dataclass(frozen=True, slots=True)
class Fired:
    """Último disparo de um gatilho cuja condição continua valendo."""

    value: float
    reason: str
    at: datetime

    def to_json(self) -> dict[str, Any]:
        return {"value": self.value, "reason": self.reason, "at": self.at.isoformat()}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Fired":
        return cls(float(data["value"]), data["reason"], datetime.fromisoformat(data["at"]))


type FiredMap = dict[tuple[str, str], Fired]
"""Disparos por (escopo, condição)."""


def escalate(current: ScopeState, proposed: ScopeState) -> ScopeState | None:
    """Novo estado para um gatilho automático, ou ``None`` se não houver mudança."""
    if proposed.state is OpState.FLATTENING and current.flattened:
        return None  # já zerado; sem novas entradas até o /resume
    if proposed.state.rank > current.state.rank:
        return proposed
    if (
        proposed.state is OpState.PAUSED
        and current.state is OpState.PAUSED
        and current.until is not None
        and (proposed.until is None or proposed.until > current.until)
    ):
        return proposed  # estende a pausa
    return None


def combined(*states: ScopeState) -> OpState:
    """Estado efetivo: o mais restritivo entre o global e o do perfil."""
    return max((s.state for s in states), key=lambda s: s.rank)


class StateStore:
    """Estados persistidos em ``checkpoints`` (sobrevivem a reinícios)."""

    def __init__(self, store: Store) -> None:
        self._store = store

    @staticmethod
    def _key(scope: str) -> str:
        return f"risk.state.{scope}"

    async def get(self, scope: str, now: datetime) -> ScopeState:
        data = await self._store.get_checkpoint(self._key(scope))
        return ScopeState.from_json(data).current(now) if data else ScopeState()

    async def put(self, scope: str, state: ScopeState) -> None:
        await self._store.set_checkpoint(self._key(scope), state.to_json())

    async def fired(self) -> FiredMap:
        data = await self._store.get_checkpoint(FIRED_KEY) or {}
        return {
            (scope, condition): Fired.from_json(fired)
            for scope, conditions in data.items()
            for condition, fired in conditions.items()
        }

    async def put_fired(self, fired: Mapping[tuple[str, str], Fired]) -> None:
        data: dict[str, dict[str, Any]] = {}
        for (scope, condition), record in sorted(fired.items()):
            data.setdefault(scope, {})[condition] = record.to_json()
        await self._store.set_checkpoint(FIRED_KEY, data)
