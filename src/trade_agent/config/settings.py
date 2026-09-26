"""Configuração via variáveis de ambiente (prefixo ``TA_``) e arquivo ``.env``."""

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from trade_agent.exchange.environments import BinanceEnvironment


class KeyType(StrEnum):
    """Tipo da chave de API da Binance."""

    ED25519 = "ed25519"
    HMAC = "hmac"


class LogFormat(StrEnum):
    CONSOLE = "console"
    JSON = "json"


type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Settings(BaseSettings):
    """Configuração de infraestrutura (credenciais, ambiente, logs).

    Parâmetros de estratégia e risco ficam em arquivos YAML versionados, não aqui.
    """

    model_config = SettingsConfigDict(
        env_prefix="TA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    binance_env: BinanceEnvironment = BinanceEnvironment.TESTNET
    binance_api_key: SecretStr | None = None
    binance_key_type: KeyType = KeyType.ED25519
    binance_private_key_path: Path | None = None
    binance_private_key_passphrase: SecretStr | None = None
    binance_api_secret: SecretStr | None = None
    binance_recv_window_ms: int = Field(default=5000, gt=0, le=60000)

    trading_enabled: bool = False

    log_level: LogLevel = "INFO"
    log_format: LogFormat = LogFormat.CONSOLE

    @field_validator(
        "binance_api_key",
        "binance_private_key_path",
        "binance_private_key_passphrase",
        "binance_api_secret",
        mode="before",
    )
    @classmethod
    def _empty_as_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _check_credentials(self) -> "Settings":
        if self.binance_api_key is None:
            return self
        if self.binance_key_type is KeyType.ED25519 and self.binance_private_key_path is None:
            raise ValueError("chave Ed25519 exige TA_BINANCE_PRIVATE_KEY_PATH")
        if self.binance_key_type is KeyType.HMAC and self.binance_api_secret is None:
            raise ValueError("chave HMAC exige TA_BINANCE_API_SECRET")
        return self

    @property
    def has_credentials(self) -> bool:
        return self.binance_api_key is not None


def load_settings(env_file: Path | str | None = ".env", **overrides: Any) -> Settings:
    """Carrega as configurações do ambiente e, se existir, do arquivo ``env_file``."""
    return Settings(_env_file=env_file, **overrides)
