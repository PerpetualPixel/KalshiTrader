"""Kalshi API request signing (RSA-PSS / SHA-256).

Each authenticated request carries three headers:
    KALSHI-ACCESS-KEY        the API key id
    KALSHI-ACCESS-TIMESTAMP  milliseconds since epoch
    KALSHI-ACCESS-SIGNATURE  base64( RSA-PSS-SHA256( timestamp + METHOD + path ) )

`path` is the URL path *including* the /trade-api/v2 prefix and *excluding* the
query string.
"""
from __future__ import annotations

import base64
import time
from pathlib import Path
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def load_private_key(path: str | Path | None = None, pem: str | None = None) -> rsa.RSAPrivateKey:
    if pem is None:
        if path is None:
            raise ValueError("either a key path or PEM text is required")
        pem = Path(path).read_text(encoding="utf-8")
    key = serialization.load_pem_private_key(pem.encode() if isinstance(pem, str) else pem, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError("Kalshi API keys must be RSA private keys")
    return key


def sign_message(private_key: rsa.RSAPrivateKey, message: str) -> str:
    signature = private_key.sign(
        message.encode("utf-8"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("ascii")


def signing_path(url: str) -> str:
    """Return the path portion Kalshi signs: full path, no query string."""
    return urlparse(url).path


class RequestSigner:
    """Signs requests, correcting for local clock drift.

    Kalshi rejects a signature whose timestamp is too far from its own clock, so a
    machine whose clock has drifted gets 401s that look like a bad key. The client
    feeds the server's `Date` header back through `sync_clock`, and every later
    signature is offset by that difference.
    """

    def __init__(self, api_key_id: str, private_key: rsa.RSAPrivateKey):
        self.api_key_id = api_key_id
        self.private_key = private_key
        self.drift_ms = 0

    def sync_clock(self, server_epoch_ms: int) -> int:
        """Record the offset between the server clock and ours. Returns the drift."""
        self.drift_ms = int(server_epoch_ms - time.time() * 1000)
        return self.drift_ms

    def now_ms(self) -> int:
        return int(time.time() * 1000) + self.drift_ms

    def headers(self, method: str, url: str, timestamp_ms: int | None = None) -> dict[str, str]:
        ts = str(timestamp_ms if timestamp_ms is not None else self.now_ms())
        message = f"{ts}{method.upper()}{signing_path(url)}"
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sign_message(self.private_key, message),
        }
