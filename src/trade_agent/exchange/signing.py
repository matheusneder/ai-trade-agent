"""Signing of Binance requests (HMAC-SHA256 and Ed25519)."""

import base64
import hashlib
import hmac
from pathlib import Path
from typing import Protocol, Self

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key


class Signer(Protocol):
    """Signs a textual payload and returns the signature in the format Binance expects."""

    def sign(self, payload: str) -> str: ...


class HmacSigner:
    """HMAC-SHA256 signature in hexadecimal."""

    __slots__ = ("_secret",)

    def __init__(self, secret: str) -> None:
        if not secret:
            raise ValueError("segredo HMAC vazio")
        self._secret = secret.encode()

    def sign(self, payload: str) -> str:
        return hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()


class Ed25519Signer:
    """Ed25519 signature encoded in base64 (the key type Binance recommends)."""

    __slots__ = ("_key",)

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._key = private_key

    @classmethod
    def from_pem(cls, pem: bytes, passphrase: bytes | None = None) -> Self:
        key = load_pem_private_key(pem, password=passphrase)
        if not isinstance(key, Ed25519PrivateKey):
            raise TypeError("a chave PEM informada não é Ed25519")
        return cls(key)

    @classmethod
    def from_pem_file(cls, path: Path, passphrase: bytes | None = None) -> Self:
        return cls.from_pem(path.read_bytes(), passphrase)

    def sign(self, payload: str) -> str:
        return base64.b64encode(self._key.sign(payload.encode())).decode()
