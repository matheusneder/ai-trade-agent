"""Ambientes da Binance Spot e seus endpoints."""

from dataclasses import dataclass
from enum import StrEnum


class BinanceEnvironment(StrEnum):
    """Ambiente de negociação."""

    TESTNET = "testnet"
    """Spot Testnet: integração da API; liquidez e preços não realistas."""

    DEMO = "demo"
    """Demo Mode: paper trading com dados de mercado realistas."""

    PROD = "prod"
    """Produção: dinheiro real."""


@dataclass(frozen=True, slots=True)
class Endpoints:
    """URLs base de um ambiente."""

    rest: str
    ws_api: str
    ws_streams: str
    sapi: bool
    """Rotas ``/sapi`` (ex.: cronograma de delistagem): a Testnet e a Demo não as têm (HTTP 404)."""


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
    """Retorna os endpoints do ambiente informado."""
    return ENDPOINTS[environment]
