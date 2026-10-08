import base64
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trade_agent.exchange.serialization import ParamValue, encode_params, ws_signature_payload
from trade_agent.exchange.signing import Ed25519Signer, HmacSigner

# Official vectors from the Binance documentation (rest-api.md and web-socket-api.md).
DOC_SECRET = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
DOC_API_KEY = "vmPUZE6mv9SD5VNHk4HlWFsOr6aKE2zvsw0MuIgwCIPy6utIco14y7Ju91duEh8A"


def _rest_params(symbol: str) -> dict[str, object]:
    return {
        "symbol": symbol,
        "side": "BUY",
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": Decimal("1"),
        "price": Decimal("0.1"),
        "recvWindow": 5000,
        "timestamp": 1499827319559,
    }


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("LTCBTC", "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"),
        ("１２３４５６", "e1353ec6b14d888f1164ae9af8228a3dbd508bc82eb867db8ab6046442f33ef3"),
    ],
)
def test_hmac_rest_matches_binance_doc_vectors(symbol: str, expected: str) -> None:
    payload = encode_params(_rest_params(symbol))  # type: ignore[arg-type]
    assert HmacSigner(DOC_SECRET).sign(payload) == expected


def test_hmac_ws_api_matches_binance_doc_vectors() -> None:
    ascii_params: dict[str, ParamValue] = {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": "0.01000000",
        "price": "52000.00",
        "recvWindow": 100,
        "timestamp": 1645423376532,
        "apiKey": DOC_API_KEY,
    }
    non_ascii_params: dict[str, ParamValue] = {
        "symbol": "１２３４５６",
        "side": "BUY",
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": "1.00000000",
        "price": "0.10000000",
        "recvWindow": 5000,
        "timestamp": 1645423376532,
        "apiKey": DOC_API_KEY,
    }
    signer = HmacSigner(DOC_SECRET)
    assert signer.sign(ws_signature_payload(ascii_params)) == (
        "aa1b5712c094bc4e57c05a1a5c1fd8d88dcd628338ea863fec7b88e59fe2db24"
    )
    assert signer.sign(ws_signature_payload(non_ascii_params)) == (
        "b33892ae8e687c939f4468c6268ddd4c40ac1af18ad19a064864c47bae0752cd"
    )


def test_hmac_rejects_empty_secret() -> None:
    with pytest.raises(ValueError, match="vazio"):
        HmacSigner("")


def _pem(key: Ed25519PrivateKey, passphrase: bytes | None = None) -> bytes:
    encryption: serialization.KeySerializationEncryption = (
        serialization.BestAvailableEncryption(passphrase)
        if passphrase
        else serialization.NoEncryption()
    )
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption
    )


def test_ed25519_signature_is_base64_and_verifiable() -> None:
    key = Ed25519PrivateKey.generate()
    signature = Ed25519Signer(key).sign("symbol=BTCUSDT&timestamp=1")
    key.public_key().verify(base64.b64decode(signature), b"symbol=BTCUSDT&timestamp=1")


def test_ed25519_from_pem_file_with_passphrase(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    path = tmp_path / "key.pem"
    path.write_bytes(_pem(key, b"s3nh4"))
    signer = Ed25519Signer.from_pem_file(path, b"s3nh4")
    key.public_key().verify(base64.b64decode(signer.sign("x")), b"x")


def test_ed25519_from_pem_rejects_other_key_types() -> None:
    ec_key = generate_private_key(SECP256R1())
    pem = ec_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    with pytest.raises(TypeError, match="Ed25519"):
        Ed25519Signer.from_pem(pem)
