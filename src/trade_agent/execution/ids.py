"""Deterministic order identifiers (``clientOrderId``/``listClientOrderId``).

Format: ``ta1-{profile}-{decision}-{seq}-{leg}``, for example ``ta1-mod-7f3a9c2b1d-0-TP``.

* ``ta1`` identifies the agent's orders (version 1 of the format); orders without this
  prefix are never touched by reconciliation;
* ``profile`` is a short code of the risk profile (``[a-z0-9]{1,12}``);
* ``decision`` identifies the decision that created the position (``[0-9a-f]{6,12}``);
* ``seq`` numbers the successive protections of the same position (0 = original OPOCO,
  1.. = OCOs recreated in adjustments);
* ``leg`` is the role of the order (list, entry, take-profit, stop, exit).

Follows Binance's rule ``^[.A-Z:/a-z0-9_-]{1,36}$``.
"""

import re
import secrets
from dataclasses import dataclass
from enum import StrEnum

APP_PREFIX = "ta1"
BINANCE_CLIENT_ID = re.compile(r"^[.A-Z:/a-z0-9_-]{1,36}$")
_PROFILE = re.compile(r"^[a-z0-9]{1,12}$")
_DECISION = re.compile(r"^[0-9a-f]{6,12}$")
_PARSE = re.compile(
    rf"^{APP_PREFIX}-(?P<profile>[a-z0-9]{{1,12}})-(?P<decision>[0-9a-f]{{6,12}})"
    r"-(?P<seq>\d{1,2})-(?P<leg>L|E|TP|SL|X)$"
)
MAX_SEQ = 99


class Leg(StrEnum):
    LIST = "L"
    ENTRY = "E"
    TAKE_PROFIT = "TP"
    STOP = "SL"
    EXIT = "X"


@dataclass(frozen=True, slots=True)
class ClientIdParts:
    profile: str
    decision: str
    seq: int
    leg: Leg


@dataclass(frozen=True, slots=True)
class OrderIdSet:
    """IDs of a protection list (OPOCO or OCO) and of a possible exit."""

    list_id: str
    entry_id: str
    take_profit_id: str
    stop_id: str
    exit_id: str


def new_decision_id() -> str:
    """New decision identifier (10 hex)."""
    return secrets.token_hex(5)


def make_client_id(profile: str, decision: str, leg: Leg, seq: int = 0) -> str:
    if not _PROFILE.match(profile):
        raise ValueError(f"código de perfil inválido: {profile!r}")
    if not _DECISION.match(decision):
        raise ValueError(f"id de decisão inválido: {decision!r}")
    if not 0 <= seq <= MAX_SEQ:
        raise ValueError(f"sequência fora do intervalo 0..{MAX_SEQ}: {seq}")
    return f"{APP_PREFIX}-{profile}-{decision}-{seq}-{leg.value}"


def order_ids(profile: str, decision: str, seq: int = 0) -> OrderIdSet:
    return OrderIdSet(
        list_id=make_client_id(profile, decision, Leg.LIST, seq),
        entry_id=make_client_id(profile, decision, Leg.ENTRY, seq),
        take_profit_id=make_client_id(profile, decision, Leg.TAKE_PROFIT, seq),
        stop_id=make_client_id(profile, decision, Leg.STOP, seq),
        exit_id=make_client_id(profile, decision, Leg.EXIT, seq),
    )


def parse_client_id(value: str) -> ClientIdParts | None:
    """Breaks down an agent ID; ``None`` for third-party IDs."""
    match = _PARSE.match(value)
    if match is None:
        return None
    return ClientIdParts(
        profile=match["profile"],
        decision=match["decision"],
        seq=int(match["seq"]),
        leg=Leg(match["leg"]),
    )


def is_agent_id(value: str) -> bool:
    return parse_client_id(value) is not None
