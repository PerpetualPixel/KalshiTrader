"""Dashboard authentication.

Single-user password auth: on first run the dashboard asks you to create a
password; afterwards it asks you to log in. The password is stored as a
PBKDF2-SHA256 hash in SQLite, and sessions are HMAC-signed expiring tokens
delivered as an httponly cookie.

An optional PIN acts as a second factor — both must be correct, and the
answer never says which one was wrong. Failed attempts are throttled: after
MAX_ATTEMPTS the next try is refused for a window that doubles each time it
is exhausted, so a reachable dashboard cannot be quietly ground down.
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

MAX_ATTEMPTS = 5  # consecutive failures before the lockout starts
BASE_LOCKOUT_SECONDS = 60  # doubles per exhausted round, to LOCKOUT_CEILING
LOCKOUT_CEILING_SECONDS = 3600
PIN_MIN_LENGTH = 4
PIN_MAX_LENGTH = 12


def _hash_password(password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)


def _record(secret: str) -> str:
    """A fresh PBKDF2 record for a password or PIN, ready to store."""
    salt = secrets.token_bytes(16)
    return json.dumps(
        {
            "salt": salt.hex(),
            "hash": _hash_password(secret, salt).hex(),
            "iterations": PBKDF2_ITERATIONS,
        }
    )


def _matches(raw: str | None, candidate: str) -> bool:
    if not raw:
        return False
    record = json.loads(raw)
    digest = _hash_password(
        candidate, bytes.fromhex(record["salt"]), record["iterations"]
    )
    return hmac.compare_digest(digest, bytes.fromhex(record["hash"]))


class AuthManager:
    def __init__(self, db: Database):
        self.db = db
        self._secret = self._load_or_create_secret()
        # Throttling state is per-process: a restart clears it, which is the
        # right trade for a single-user dashboard — an operator locked out by
        # their own typo can always restart, and an attacker cannot.
        self._failures = 0
        self._locked_until = 0.0

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
        self.db.set_kv("auth_password", _record(password))

    def verify_password(self, password: str) -> bool:
        return _matches(self.db.get_kv("auth_password"), password)

    # ── PIN (optional second factor) ──────────────────────────────────

    def has_pin(self) -> bool:
        return self.db.get_kv("auth_pin") is not None

    def set_pin(self, pin: str) -> None:
        if not pin.isdigit():
            raise ValueError("PIN must be digits only")
        if not PIN_MIN_LENGTH <= len(pin) <= PIN_MAX_LENGTH:
            raise ValueError(
                f"PIN must be {PIN_MIN_LENGTH}-{PIN_MAX_LENGTH} digits"
            )
        self.db.set_kv("auth_pin", _record(pin))

    def clear_pin(self) -> None:
        self.db.delete_kv("auth_pin")

    def verify_pin(self, pin: str | None) -> bool:
        if not self.has_pin():
            return True  # no second factor configured
        return _matches(self.db.get_kv("auth_pin"), pin or "")

    def verify_credentials(self, password: str, pin: str | None) -> bool:
        """Check both factors, always evaluating both.

        Verifying the PIN even when the password is already wrong keeps the
        work — and so the response time — the same either way, so a caller
        cannot learn which factor failed by timing the answer.
        """
        password_ok = self.verify_password(password)
        pin_ok = self.verify_pin(pin)
        return password_ok and pin_ok

    # ── Login throttling ──────────────────────────────────────────────

    def lockout_remaining(self) -> int:
        """Seconds until logins are accepted again; 0 when not locked out."""
        return max(0, int(self._locked_until - time.time()))

    def register_failure(self) -> None:
        self._failures += 1
        if self._failures % MAX_ATTEMPTS == 0:
            rounds = self._failures // MAX_ATTEMPTS
            delay = min(
                BASE_LOCKOUT_SECONDS * (2 ** (rounds - 1)), LOCKOUT_CEILING_SECONDS
            )
            self._locked_until = time.time() + delay

    def register_success(self) -> None:
        self._failures = 0
        self._locked_until = 0.0

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
