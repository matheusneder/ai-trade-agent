import json

import pytest
import structlog

from trade_agent.config.settings import LogFormat
from trade_agent.exchange.environments import ENDPOINTS, BinanceEnvironment, endpoints_for
from trade_agent.log import configure_logging


def test_every_environment_has_endpoints() -> None:
    assert set(ENDPOINTS) == set(BinanceEnvironment)
    assert endpoints_for(BinanceEnvironment.DEMO).rest == "https://demo-api.binance.com"
    assert endpoints_for(BinanceEnvironment.TESTNET).ws_api.startswith("wss://ws-api.testnet")
    for endpoints in ENDPOINTS.values():
        assert endpoints.rest.startswith("https://")
        assert endpoints.ws_api.startswith("wss://")
        assert endpoints.ws_streams.startswith("wss://")
    # as rotas /sapi (cronograma de delistagem) só existem na produção
    assert {env for env, endpoints in ENDPOINTS.items() if endpoints.sapi} == {
        BinanceEnvironment.PROD
    }


def test_json_logging(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", LogFormat.JSON)
    structlog.get_logger().info("evento", par="BTCUSDT")
    structlog.get_logger().debug("filtrado")
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "evento"
    assert record["par"] == "BTCUSDT"
    assert record["level"] == "info"


def test_console_logging(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("DEBUG", LogFormat.CONSOLE)
    structlog.get_logger().debug("detalhe", n=1)
    assert "detalhe" in capsys.readouterr().out
