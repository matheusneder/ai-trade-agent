"""Binance Spot environments and their endpoints."""

from dataclasses import dataclass
from enum import StrEnum


class BinanceEnvironment(StrEnum):
    """Trading environment."""

    TESTNET = "testnet"
    """Spot Testnet: API integration; unrealistic liquidity and prices."""

    DEMO = "demo"
    """Demo Mode: paper trading with realistic market data."""

    PROD = "prod"
    """Production: real money."""


@dataclass(frozen=True, slots=True)
class Endpoints:
    """Base URLs of an environment."""

    rest: str
    ws_api: str
    ws_streams: str
    sapi: bool
    """``/sapi`` routes (e.g. delisting schedule): Testnet and Demo do not have them (HTTP 404)."""


ENDPOINTS: dict[BinanceEnvironment, Endpoints] = {
    BinanceEnvironment.TESTNET: Endpoints(
        rest="https://testnet.binance.vision",
        ws_api="wss://ws-api.testnet.binance.vision/ws-api/v3",
        ws_streams="wss://stream.testnet.binance.vision/stream",
        sapi=False,
    ),
    BinanceEnvironment.DEMO: Endpoints(
        rest="https://demo-api.binance.com",
        ws_api="wss://demo-ws-api.binance.com/ws-api/v3",
        ws_streams="wss://demo-stream.binance.com/stream",
        sapi=False,
    ),
    BinanceEnvironment.PROD: Endpoints(
        rest="https://api.binance.com",
        ws_api="wss://ws-api.binance.com:443/ws-api/v3",
        ws_streams="wss://stream.binance.com:9443/stream",
        sapi=True,
    ),
}


def endpoints_for(environment: BinanceEnvironment) -> Endpoints:
    """Returns the endpoints of the given environment."""
    return ENDPOINTS[environment]
