"""Identificadores determinísticos de ordens (``clientOrderId``/``listClientOrderId``).

Formato: ``ta1-{perfil}-{decisão}-{seq}-{perna}``, por exemplo ``ta1-mod-7f3a9c2b1d-0-TP``.

* ``ta1`` identifica ordens do agente (versão 1 do formato); ordens sem esse prefixo
  nunca são tocadas pela reconciliação;
* ``perfil`` é um código curto do perfil de risco (``[a-z0-9]{1,12}``);
* ``decisão`` identifica a decisão que originou a posição (``[0-9a-f]{6,12}``);
* ``seq`` numera as proteções sucessivas da mesma posição (0 = OPOCO original,
  1.. = OCOs recriados em ajustes);
* ``perna`` indica o papel da ordem (lista, entrada, take-profit, stop, saída).

Respeita a regra da Binance ``^[.A-Z:/a-z0-9_-]{1,36}$``.
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
    """IDs de uma lista de proteção (OPOCO ou OCO) e de uma eventual saída."""

    list_id: str
    entry_id: str
    take_profit_id: str
    stop_id: str
    exit_id: str


def new_decision_id() -> str:
    """Novo identificador de decisão (10 hex)."""
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
    """Decompõe um ID do agente; ``None`` para IDs de terceiros."""
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
