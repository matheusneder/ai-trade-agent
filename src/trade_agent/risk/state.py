"""Operating states (doc 03, §10.2), global and per profile, persisted in the database.

| State         | New entries    | Protection| Rule-based exits | Return                     |
|---------------|----------------|-----------|------------------|----------------------------|
| ``RUNNING``   | yes            | yes       | yes              | —                          |
| ``PAUSED``    | no             | yes       | yes              | after the cooldown/manual  |
| ``FLATTENING``| no             | sells     | —                | goes to ``HALTED``         |
| ``HALTED``    | no             | kept      | no               | manual only (``/resume``)  |

Automatic triggers only **escalate** (they never relax a more restrictive state); the
return to ``RUNNING`` is manual, except for a pause whose cooldown expired. Each trigger
acts once per occurrence (``Fired``): while the condition lasts, ``/resume`` and the end
of the cooldown hold.
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
    """End of a pause's cooldown (``None``: manual only)."""
    flattened: bool = False
    """``HALTED`` that resulted from a flatten: the same trigger does not repeat it."""

    def current(self, now: datetime) -> "ScopeState":
        """A pause whose cooldown expired returns to ``RUNNING``."""
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
    """Last firing of a trigger whose condition still holds."""

    value: float
    reason: str
    at: datetime

    def to_json(self) -> dict[str, Any]:
        return {"value": self.value, "reason": self.reason, "at": self.at.isoformat()}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Fired":
        return cls(float(data["value"]), data["reason"], datetime.fromisoformat(data["at"]))


type FiredMap = dict[tuple[str, str], Fired]
"""Firings by (scope, condition)."""


def escalate(current: ScopeState, proposed: ScopeState) -> ScopeState | None:
    """New state for an automatic trigger, or ``None`` if nothing changes."""
    if proposed.state is OpState.FLATTENING and current.flattened:
        return None  # already flat; no new entries until /resume
    if proposed.state.rank > current.state.rank:
        return proposed
    if (
        proposed.state is OpState.PAUSED
        and current.state is OpState.PAUSED
        and current.until is not None
        and (proposed.until is None or proposed.until > current.until)
    ):
        return proposed  # extends the pause
    return None


def combined(*states: ScopeState) -> OpState:
    """Effective state: the most restrictive of the global one and the profile's."""
    return max((s.state for s in states), key=lambda s: s.rank)


class StateStore:
    """States persisted in ``checkpoints`` (they survive restarts)."""

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
