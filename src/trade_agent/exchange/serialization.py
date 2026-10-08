"""Serialization of parameters for the Binance API.

Prices and quantities travel as :class:`~decimal.Decimal` and are sent in fixed decimal
notation, without an exponent: Binance rejects ``1E-5`` (error ``-1100 ILLEGAL_CHARS``).
"""

from collections.abc import Mapping
from decimal import Decimal
from enum import Enum
from urllib.parse import quote

type ParamValue = str | int | Decimal | bool | Enum | None


def format_decimal(value: Decimal) -> str:
    """Formats a Decimal in fixed notation, without trailing zeros or an exponent.

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
    """Converts a parameter value to the textual form the API expects."""
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
    """Drops ``None`` parameters and converts the rest to text, preserving the order."""
    return {key: to_param_str(value) for key, value in params.items() if value is not None}


def encode_params(params: Mapping[str, ParamValue]) -> str:
    """Builds the percent-encoded query string (signed payload of the REST API).

    The parameter order is preserved and values are fully encoded
    (``quote(..., safe="")``), exactly as they will be sent and signed.
    """
    return "&".join(
        f"{quote(key, safe='')}={quote(value, safe='')}"
        for key, value in clean_params(params).items()
    )


def ws_signature_payload(params: Mapping[str, ParamValue]) -> str:
    """Signature payload of the WebSocket API: parameters sorted by name, without encoding."""
    cleaned = clean_params(params)
    return "&".join(f"{key}={cleaned[key]}" for key in sorted(cleaned))
