"""Dashboard authentication.

Single-user password auth: on first run the dashboard asks you to create a
password; afterwards it asks you to log in. The password is stored as a
PBKDF2-SHA256 hash in SQLite, and sessions are HMAC-signed expiring tokens
delivered as an httponly cookie.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time

from .database import Database

PBKDF2_ITERATIONS = 300_000
SESSION_TTL_SECONDS = 7 * 24 * 3600
COOKIE_NAME = "kalshitrader_session"


def _hash_password(password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)


class AuthManager:
    def __init__(self, db: Database):
        self.db = db
        self._secret = self._load_or_create_secret()

    # ── Storage helpers ───────────────────────────────────────────────

    def _load_or_create_secret(self) -> bytes:
        raw = self.db.get_kv("session_secret")
        if raw:
            return bytes.fromhex(raw)
        secret = secrets.token_bytes(32)
        self.db.set_kv("session_secret", secret.hex())
        return secret

    # ── Password lifecycle ────────────────────────────────────────────

    def is_configured(self) -> bool:
        return self.db.get_kv("auth_password") is not None

    def set_password(self, password: str) -> None:
        if len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        salt = secrets.token_bytes(16)
        digest = _hash_password(password, salt)
        self.db.set_kv(
            "auth_password",
            json.dumps(
                {
                    "salt": salt.hex(),
                    "hash": digest.hex(),
                    "iterations": PBKDF2_ITERATIONS,
                }
            ),
        )

    def verify_password(self, password: str) -> bool:
        raw = self.db.get_kv("auth_password")
        if not raw:
            return False
        record = json.loads(raw)
        digest = _hash_password(
            password, bytes.fromhex(record["salt"]), record["iterations"]
        )
        return hmac.compare_digest(digest, bytes.fromhex(record["hash"]))

    # ── Session tokens ────────────────────────────────────────────────

    def issue_token(self) -> str:
        expires = int(time.time()) + SESSION_TTL_SECONDS
        payload = str(expires)
        sig = hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{sig}"

    def verify_token(self, token: str | None) -> bool:
        if not token or "." not in token:
            return False
        payload, _, sig = token.rpartition(".")
        expected = hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        try:
            return int(payload) > time.time()
        except ValueError:
            return False

    def revoke_all_sessions(self) -> None:
        """Rotate the signing secret, invalidating every outstanding token."""
        self._secret = secrets.token_bytes(32)
        self.db.set_kv("session_secret", self._secret.hex())
