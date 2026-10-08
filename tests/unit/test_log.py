import logging

import httpx
import pytest
import structlog
from structlog.testing import capture_logs

from trade_agent.config.settings import LogFormat
from trade_agent.exchange.errors import BinanceConnectionError
from trade_agent.exchange.rest import BinanceRestClient
from trade_agent.exchange.signing import HmacSigner
from trade_agent.log import QUIET_LIBRARIES, REDACTED, configure_logging, redact_secrets


def test_redacts_sensitive_fields_but_not_token_counts() -> None:
    event = {
        "api_key": "abc",
        "apikey": "abc",
        "bot_token": "123:xyz",
        "signature": "deadbeef",
        "db_password": "x",
        "private_key": "pem",
        "input_tokens": 1200,
        "cache_read_input_tokens": 10,
        "symbol": "BTCUSDT",
    }
    result = redact_secrets(None, "debug", dict(event))
    for key in ("api_key", "apikey", "bot_token", "signature", "db_password", "private_key"):
        assert result[key] == REDACTED
    assert result["input_tokens"] == 1200 and result["cache_read_input_tokens"] == 10
    assert result["symbol"] == "BTCUSDT"


def test_scrubs_secret_patterns_inside_text() -> None:
    event = {
        "error": "GET https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/getUpdates",
        "detail": "chave sk-ant-api03-AbC_dEf-123 recusada",
        "url": "/api/v3/order?symbol=BTCUSDT&timestamp=1&signature=0a1b2c",
        "count": 3,
    }
    result = redact_secrets(None, "error", dict(event))
    assert result["error"] == f"GET https://api.telegram.org/bot{REDACTED}/getUpdates"
    assert result["detail"] == f"chave {REDACTED} recusada"
    assert result["url"] == f"/api/v3/order?symbol=BTCUSDT&timestamp=1&signature={REDACTED}"
    assert result["count"] == 3


@pytest.mark.parametrize("fmt", [LogFormat.CONSOLE, LogFormat.JSON])
def test_debug_level_controls_verbosity(fmt: LogFormat, capsys: pytest.CaptureFixture[str]) -> None:
    log = structlog.get_logger("teste")
    configure_logging("INFO", fmt)
    log.debug("detalhe.oculto")
    log.info("resumo.visivel", api_key="segredo")
    configure_logging("DEBUG", fmt)
    log.debug("detalhe.visivel")
    output = capsys.readouterr().out
    assert "detalhe.oculto" not in output
    assert "resumo.visivel" in output and "detalhe.visivel" in output
    assert "segredo" not in output
    for name in QUIET_LIBRARIES:  # full URLs (with tokens) never show up in the logs
        assert logging.getLogger(name).level == logging.WARNING


async def test_rest_debug_log_and_errors_never_expose_query_or_signature() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/account":
            return httpx.Response(200, json={"balances": []}, headers={"X-MBX-USED-WEIGHT-1M": "7"})
        raise httpx.ConnectError("sem rede")

    async with httpx.AsyncClient(
        base_url="https://fake", transport=httpx.MockTransport(handler)
    ) as http:
        client = BinanceRestClient(
            "https://fake", api_key="CHAVE-SECRETA", signer=HmacSigner("s3cr3t"), http_client=http
        )
        with capture_logs() as logs:
            await client.signed("GET", "/api/v3/account", {"omitZeroBalances": "true"})
            with pytest.raises(BinanceConnectionError) as info:
                await client.signed("GET", "/api/v3/myTrades", {"symbol": "BTCUSDT"})
    request, failed = logs
    assert request["event"] == "rest.request" and request["log_level"] == "debug"
    assert (request["method"], request["path"], request["status"]) == (
        "GET",
        "/api/v3/account",
        200,
    )
    assert request["signed"] is True and request["weight_1m"] == 7
    assert request["elapsed_ms"] >= 0
    assert (failed["event"], failed["path"], failed["error"]) == (
        "rest.request_failed",
        "/api/v3/myTrades",
        "ConnectError",
    )
    text = f"{logs} {info.value}"
    assert "signature" not in text and "CHAVE-SECRETA" not in text and "BTCUSDT" not in text
