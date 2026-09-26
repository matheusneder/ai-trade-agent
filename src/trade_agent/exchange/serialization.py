"""Serialização de parâmetros para a API da Binance.

Preços e quantidades trafegam como :class:`~decimal.Decimal` e são enviados em notação
decimal fixa, sem expoente: a Binance rejeita ``1E-5`` (erro ``-1100 ILLEGAL_CHARS``).
"""

from collections.abc import Mapping
from decimal import Decimal
from enum import Enum
from urllib.parse import quote

type ParamValue = str | int | Decimal | bool | Enum | None


def format_decimal(value: Decimal) -> str:
    """Formata um Decimal em notação fixa, sem zeros à direita e sem expoente.

    >>> format_decimal(Decimal("1E-5"))
    '0.00001'
    >>> format_decimal(Decimal("150.2500"))
    '150.25'
    """
    if not value.is_finite():
        raise ValueError(f"valor decimal não finito: {value}")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text == "-0":
        return "0"
    return text


def to_param_str(value: str | int | Decimal | bool | Enum) -> str:
    """Converte um valor de parâmetro para a representação textual esperada pela API."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, Decimal):
        return format_decimal(value)
    if isinstance(value, int):
        return str(value)
    return value


def clean_params(params: Mapping[str, ParamValue]) -> dict[str, str]:
    """Remove parâmetros ``None`` e converte os demais para texto, preservando a ordem."""
    return {key: to_param_str(value) for key, value in params.items() if value is not None}


def encode_params(params: Mapping[str, ParamValue]) -> str:
    """Monta a query string percent-encoded (payload assinado da REST API).

    A ordem dos parâmetros é preservada e os valores são codificados por completo
    (``quote(..., safe="")``), exatamente como serão enviados e assinados.
    """
    return "&".join(
        f"{quote(key, safe='')}={quote(value, safe='')}"
        for key, value in clean_params(params).items()
    )


def ws_signature_payload(params: Mapping[str, ParamValue]) -> str:
    """Payload de assinatura da WebSocket API: parâmetros ordenados por nome, sem encoding."""
    cleaned = clean_params(params)
    return "&".join(f"{key}={cleaned[key]}" for key in sorted(cleaned))
