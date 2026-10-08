"""Builds the signer and the REST client from the settings."""

from typing import Any

from trade_agent.config.settings import KeyType, Settings
from trade_agent.exchange.environments import endpoints_for
from trade_agent.exchange.errors import BinanceConfigurationError
from trade_agent.exchange.rest import BinanceRestClient
from trade_agent.exchange.signing import Ed25519Signer, HmacSigner, Signer


def build_signer(settings: Settings) -> Signer | None:
    """Creates the signer for the key type; ``None`` if there are no credentials."""
    if not settings.has_credentials:
        return None
    if settings.binance_key_type is KeyType.HMAC:
        if settings.binance_api_secret is None:
            raise BinanceConfigurationError("chave HMAC exige TA_BINANCE_API_SECRET")
        return HmacSigner(settings.binance_api_secret.get_secret_value())
    if settings.binance_private_key_path is None:
        raise BinanceConfigurationError("chave Ed25519 exige TA_BINANCE_PRIVATE_KEY_PATH")
    passphrase = settings.binance_private_key_passphrase
    return Ed25519Signer.from_pem_file(
        settings.binance_private_key_path,
        passphrase.get_secret_value().encode() if passphrase else None,
    )


def build_rest_client(settings: Settings, **kwargs: Any) -> BinanceRestClient:
    """Creates the REST client of the configured environment."""
    api_key = settings.binance_api_key.get_secret_value() if settings.binance_api_key else None
    return BinanceRestClient(
        endpoints_for(settings.binance_env).rest,
        api_key=api_key,
        signer=build_signer(settings),
        recv_window_ms=settings.binance_recv_window_ms,
        trading_enabled=settings.trading_enabled,
        **kwargs,
    )
