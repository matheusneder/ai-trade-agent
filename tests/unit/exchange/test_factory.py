import base64
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr

from trade_agent.config.settings import KeyType, Settings, load_settings
from trade_agent.exchange.environments import BinanceEnvironment
from trade_agent.exchange.errors import BinanceConfigurationError
from trade_agent.exchange.factory import build_rest_client, build_signer
from trade_agent.exchange.signing import Ed25519Signer, HmacSigner


def _write_key(path: Path, passphrase: bytes | None = None) -> Ed25519PrivateKey:
    key = Ed25519PrivateKey.generate()
    encryption: serialization.KeySerializationEncryption = (
        serialization.BestAvailableEncryption(passphrase)
        if passphrase
        else serialization.NoEncryption()
    )
    path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption)
    )
    return key


def test_no_credentials_means_no_signer() -> None:
    assert build_signer(load_settings(env_file=None)) is None


def test_hmac_signer() -> None:
    settings = load_settings(
        env_file=None, binance_api_key="k", binance_key_type="hmac", binance_api_secret="s"
    )
    assert isinstance(build_signer(settings), HmacSigner)


@pytest.mark.parametrize("passphrase", [None, b"pw"])
def test_ed25519_signer_from_file(tmp_path: Path, passphrase: bytes | None) -> None:
    key = _write_key(tmp_path / "k.pem", passphrase)
    settings = load_settings(
        env_file=None,
        binance_api_key="k",
        binance_private_key_path=tmp_path / "k.pem",
        binance_private_key_passphrase=passphrase.decode() if passphrase else None,
    )
    signer = build_signer(settings)
    assert isinstance(signer, Ed25519Signer)
    key.public_key().verify(base64.b64decode(signer.sign("p")), b"p")


def test_inconsistent_settings_are_rejected_defensively() -> None:
    hmac_without_secret = Settings.model_construct(
        binance_api_key=SecretStr("k"),
        binance_key_type=KeyType.HMAC,
        binance_api_secret=None,
    )
    ed_without_path = Settings.model_construct(
        binance_api_key=SecretStr("k"),
        binance_key_type=KeyType.ED25519,
        binance_private_key_path=None,
    )
    for settings in (hmac_without_secret, ed_without_path):
        with pytest.raises(BinanceConfigurationError):
            build_signer(settings)


async def test_build_rest_client_uses_environment_and_flags() -> None:
    settings = load_settings(
        env_file=None,
        binance_env=BinanceEnvironment.DEMO,
        binance_api_key="k",
        binance_key_type="hmac",
        binance_api_secret="s",
        trading_enabled=True,
    )
    async with build_rest_client(settings) as client:
        assert client.trading_enabled is True
        assert str(client._http.base_url) == "https://demo-api.binance.com"
        assert client._api_key == "k"


async def test_build_rest_client_without_credentials() -> None:
    async with build_rest_client(load_settings(env_file=None)) as client:
        assert client._api_key is None
        assert client.trading_enabled is False
