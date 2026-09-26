from pathlib import Path

import pytest
from pydantic import ValidationError

from trade_agent.config.settings import KeyType, LogFormat, Settings, load_settings
from trade_agent.exchange.environments import BinanceEnvironment


def test_defaults_without_env_file() -> None:
    settings = load_settings(env_file=None)
    assert settings.binance_env is BinanceEnvironment.TESTNET
    assert settings.trading_enabled is False
    assert settings.has_credentials is False
    assert settings.binance_key_type is KeyType.ED25519
    assert settings.log_level == "INFO"
    assert settings.log_format is LogFormat.CONSOLE


def test_reads_env_file_and_treats_blank_values_as_none(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TA_BINANCE_ENV=demo\n"
        "TA_BINANCE_API_KEY=abc\n"
        "TA_BINANCE_PRIVATE_KEY_PATH=./k.pem\n"
        "TA_BINANCE_PRIVATE_KEY_PASSPHRASE=\n"
        "TA_BINANCE_API_SECRET=   \n"
        "TA_TRADING_ENABLED=true\n"
        "TA_LOG_LEVEL=debug\n",
        encoding="utf-8",
    )
    settings = load_settings(env_file=env_file)
    assert settings.binance_env is BinanceEnvironment.DEMO
    assert settings.binance_api_key is not None
    assert settings.binance_api_key.get_secret_value() == "abc"
    assert settings.binance_private_key_path == Path("./k.pem")
    assert settings.binance_private_key_passphrase is None
    assert settings.binance_api_secret is None
    assert settings.trading_enabled is True
    assert settings.log_level == "DEBUG"
    assert settings.has_credentials is True


def test_environment_variables_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TA_BINANCE_ENV", "prod")
    assert load_settings(env_file=None).binance_env is BinanceEnvironment.PROD


def test_ed25519_requires_private_key_path() -> None:
    with pytest.raises(ValidationError, match="TA_BINANCE_PRIVATE_KEY_PATH"):
        load_settings(env_file=None, binance_api_key="abc")


def test_hmac_requires_secret() -> None:
    with pytest.raises(ValidationError, match="TA_BINANCE_API_SECRET"):
        load_settings(env_file=None, binance_api_key="abc", binance_key_type="hmac")


def test_hmac_with_secret_is_valid() -> None:
    settings = load_settings(
        env_file=None, binance_api_key="abc", binance_key_type="hmac", binance_api_secret="s"
    )
    assert settings.binance_key_type is KeyType.HMAC


@pytest.mark.parametrize("value", [0, 60001])
def test_recv_window_bounds(value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, binance_recv_window_ms=value)  # type: ignore[call-arg]


def test_invalid_log_level() -> None:
    with pytest.raises(ValidationError):
        load_settings(env_file=None, log_level=10)
