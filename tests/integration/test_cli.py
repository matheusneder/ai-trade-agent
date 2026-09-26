"""CLI de operação manual contra a Binance simulada."""

import io
import json
import runpy
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.support.clients import fake_api
from tests.support.fake_binance import FakeBinance, Fault
from trade_agent.cli import build_parser, main
from trade_agent.config.settings import Settings
from trade_agent.exchange.api import BinanceSpotApi

D = Decimal
PROTECTION = ["--tp-pct", "3", "--tp-trailing-bips", "100", "--stop-pct", "4"]


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text("TA_BINANCE_ENV=testnet\nTA_LOG_LEVEL=WARNING\n", encoding="utf-8")
    return path


def _run(fake: FakeBinance, env_file: Path, *argv: str) -> tuple[int, Any, str]:
    @asynccontextmanager
    async def factory(_: Settings) -> AsyncIterator[BinanceSpotApi]:
        async with fake_api(fake) as api:
            yield api

    out, err = io.StringIO(), io.StringIO()
    code = main(["--env-file", str(env_file), *argv], out=out, err=err, api_factory=factory)
    text = out.getvalue()
    return code, json.loads(text) if text else None, err.getvalue()


def test_info_account_and_lists(env_file: Path) -> None:
    fake = FakeBinance()
    code, data, _ = _run(fake, env_file, "info", "BTCUSDT")
    assert code == 0
    assert data["ask"] == "63000"
    assert data["rules"]["tick_size"] == "0.01000000"
    assert data["rules"]["opo_allowed"] is True
    code, data, _ = _run(fake, env_file, "account")
    assert data == {"USDT": {"free": "10000", "locked": "0"}}
    code, data, _ = _run(fake, env_file, "lists")
    assert data == []


def test_open_with_fok_then_list_and_close(env_file: Path) -> None:
    fake = FakeBinance()
    code, data, err = _run(fake, env_file, "open", "BTCUSDT", "--quote", "100", *PROTECTION)
    assert code == 0, err
    assert data["listOrderStatus"] == "EXECUTING"
    list_id = data["listClientOrderId"]
    assert list_id.startswith("ta1-man-")
    tp = fake.order_by_client_id(list_id.replace("-L", "-TP"))
    assert tp is not None and tp.is_open
    assert tp.stop_price == D("65019.78")  # ask 63000 +0,2% de slippage → 63126 × 1,03
    code, data, _ = _run(fake, env_file, "lists")
    assert [ol["listClientOrderId"] for ol in data] == [list_id]
    code, data, _ = _run(
        fake, env_file, "close", "BTCUSDT", "--qty", str(tp.orig_qty), "--list-id", list_id
    )
    assert code == 0
    assert data["status"] == "FILLED"
    assert fake.open_lists() == []


def test_open_with_maker_price_and_trailing_stop(env_file: Path) -> None:
    fake = FakeBinance()
    args = ["--tp-pct", "5", "--tp-limit", "--trailing-stop-bips", "300"]
    code, data, err = _run(
        fake, env_file, "open", "BTCUSDT", "--quote", "100", "--maker-price", "62000", *args
    )
    assert code == 0, err
    working = next(o for o in data["orderReports"] if o["side"] == "BUY")
    assert working["type"] == "LIMIT_MAKER"
    assert fake.balance("USDT")[1] > 0


def test_protect_existing_balance(env_file: Path) -> None:
    fake = FakeBinance(balances={"USDT": D("100"), "BTC": D("0.002")})
    code, data, err = _run(fake, env_file, "protect", "BTCUSDT", "--qty", "0.002", *PROTECTION)
    assert code == 0, err
    assert data["contingencyType"] == "OCO"
    assert fake.balance("BTC") == (D("0"), D("0.002"))


def test_close_when_protection_already_executed(env_file: Path) -> None:
    fake = FakeBinance()
    _, data, _ = _run(fake, env_file, "open", "BTCUSDT", "--quote", "100", *PROTECTION)
    fake.set_price("BTCUSDT", D("59000"))
    code, result, _ = _run(
        fake, env_file, "close", "BTCUSDT", "--qty", "0.001", "--list-id", data["listClientOrderId"]
    )
    assert code == 0
    assert result == {"already_closed": True}


def test_validation_error_is_reported(env_file: Path) -> None:
    code, _, err = _run(FakeBinance(), env_file, "open", "BTCUSDT", "--quote", "1", *PROTECTION)
    assert code == 1
    assert "Ordem inválida" in err
    assert "NOTIONAL" in err


def test_unknown_outcome_is_reported(env_file: Path) -> None:
    fake = FakeBinance()
    fake.inject(Fault("POST", "/api/v3/orderList/opoco", "timeout_before"))
    for _ in range(3):
        fake.inject(Fault("GET", "/api/v3/orderList", "timeout_before"))
    code, _, err = _run(fake, env_file, "open", "BTCUSDT", "--quote", "100", *PROTECTION)
    assert code == 1
    assert "Resultado desconhecido" in err


def test_exchange_error_and_invalid_policy_are_reported(env_file: Path) -> None:
    fake = FakeBinance()
    fake.inject(Fault("GET", "/api/v3/account", "reject", code=-2015, message="Invalid API-key"))
    code, _, err = _run(fake, env_file, "account")
    assert code == 1 and "Invalid API-key" in err
    code, _, err = _run(
        fake, env_file, "open", "BTCUSDT", "--quote", "100", "--tp-pct", "3", "--stop-pct", "4"
    )
    assert code == 1 and "take_profit_trailing_bips" in err


def test_prod_requires_confirmation(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TA_BINANCE_ENV=prod\n", encoding="utf-8")
    code, _, err = _run(FakeBinance(), env_file, "open", "BTCUSDT", "--quote", "100", *PROTECTION)
    assert code == 2
    assert "--confirm-prod" in err
    code, data, _ = _run(FakeBinance(), env_file, "account")  # consultas não exigem confirmação
    assert code == 0 and "USDT" in data
    code, data, err = _run(
        FakeBinance(), env_file, "open", "BTCUSDT", "--quote", "100", *PROTECTION, "--confirm-prod"
    )
    assert code == 0, err


def test_parser_requires_stop_choice() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["open", "BTCUSDT", "--quote", "10", "--tp-pct", "3"])


def test_module_entry_point_help(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["trade-agent", "--help"])
    with pytest.raises(SystemExit) as info:
        runpy.run_module("trade_agent", run_name="__main__")
    assert info.value.code == 0


async def test_default_api_factory_syncs_time(
    env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    from trade_agent import cli

    fake = FakeBinance()
    real_client = httpx.AsyncClient

    def patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = fake.transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    settings = Settings(_env_file=None)
    async with cli._default_api(settings) as api:
        assert abs(api.rest.now_ms() - fake.clock()) < 5_000  # relógio alinhado ao servidor
        assert (await api.book_ticker("BTCUSDT")).ask_price == D("63000")
    assert fake.calls("GET", "/api/v3/time")
