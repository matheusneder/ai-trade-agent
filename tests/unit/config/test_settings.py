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
        Settings(_env_file=None, binance_recv_window_ms=value)


def test_the_tape_is_on_by_default_with_its_retention_and_a_derived_window() -> None:
    settings = load_settings(env_file=None)
    assert settings.tape_enabled is True
    assert settings.tape_quote_retention_days == 120
    assert settings.tape_watch_window_days is None


def test_the_tape_settings_read_the_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    lines = [
        "TA_TAPE_ENABLED=false",
        "TA_TAPE_QUOTE_RETENTION_DAYS=30",
        "TA_TAPE_WATCH_WINDOW_DAYS=",
    ]
    env_file.write_text("\n".join(lines), encoding="utf-8")
    settings = load_settings(env_file=env_file)
    assert settings.tape_enabled is False
    assert settings.tape_quote_retention_days == 30
    assert settings.tape_watch_window_days is None  # blank: derived from the profiles
    assert load_settings(env_file=None, tape_watch_window_days=7).tape_watch_window_days == 7


@pytest.mark.parametrize("bad", [{"tape_quote_retention_days": 0}, {"tape_watch_window_days": 0}])
def test_the_tape_settings_refuse_a_window_that_keeps_nothing(bad: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        load_settings(env_file=None, **bad)


def test_invalid_log_level() -> None:
    with pytest.raises(ValidationError):
        load_settings(env_file=None, log_level=10)
