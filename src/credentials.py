"""API credential storage for the dashboard.

Credentials entered in the GUI take priority over .env values. The key ID is
kept in SQLite; the private key PEM is written to keys/<env>_key.pem with
0600 permissions (the keys/ directory is gitignored). Both sources feed
`credentials_for(env)`, which the engine uses to build its API client.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from .config import EnvConfig
from .database import Database


class CredentialStore:
    def __init__(self, db: Database, env_config: EnvConfig, keys_dir: str = "./keys"):
        self.db = db
        self.env_config = env_config
        self.keys_dir = Path(keys_dir)

    def _kv_key(self, env: str) -> str:
        return f"cred_{env}"

    def credentials_for(self, env: str) -> tuple[str, str]:
        """Return (key_id, pem_path). GUI-saved credentials win over .env."""
        raw = self.db.get_kv(self._kv_key(env))
        if raw:
            record = json.loads(raw)
            if record.get("key_id") and Path(record.get("pem_path", "")).exists():
                return record["key_id"], record["pem_path"]
        return self.env_config.credentials_for(env)

    def save(self, env: str, key_id: str, private_key_pem: str) -> str:
        """Validate and persist credentials; returns the stored PEM path."""
        if env not in ("demo", "live"):
            raise ValueError("env must be 'demo' or 'live'")
        key_id = key_id.strip()
        if not key_id:
            raise ValueError("key_id is required")
        pem_bytes = private_key_pem.strip().encode() + b"\n"
        key = serialization.load_pem_private_key(pem_bytes, password=None)
        if not isinstance(key, RSAPrivateKey):
            raise ValueError("the provided key is not an RSA private key")

        self.keys_dir.mkdir(parents=True, exist_ok=True)
        pem_path = self.keys_dir / f"{env}_key.pem"
        pem_path.write_bytes(pem_bytes)
        os.chmod(pem_path, 0o600)

        self.db.set_kv(
            self._kv_key(env), json.dumps({"key_id": key_id, "pem_path": str(pem_path)})
        )
        return str(pem_path)

    def clear(self, env: str) -> None:
        raw = self.db.get_kv(self._kv_key(env))
        if raw:
            record = json.loads(raw)
            pem_path = Path(record.get("pem_path", ""))
            if pem_path.exists() and pem_path.parent == self.keys_dir:
                pem_path.unlink()
        self.db.set_kv(self._kv_key(env), "")

    @staticmethod
    def _mask(key_id: str) -> str:
        if len(key_id) <= 8:
            return key_id[:2] + "…"
        return f"{key_id[:4]}…{key_id[-4:]}"

    def describe(self) -> dict[str, dict]:
        """Masked status for the GUI — never returns key material."""
        out: dict[str, dict] = {}
        for env in ("demo", "live"):
            raw = self.db.get_kv(self._kv_key(env))
            record = json.loads(raw) if raw else {}
            if record.get("key_id"):
                out[env] = {
                    "configured": Path(record.get("pem_path", "")).exists(),
                    "key_id_masked": self._mask(record["key_id"]),
                    "source": "gui",
                }
            else:
                key_id, pem_path = self.env_config.credentials_for(env)
                configured = bool(key_id) and Path(pem_path).exists()
                out[env] = {
                    "configured": configured,
                    "key_id_masked": self._mask(key_id) if key_id else "",
                    "source": "env" if key_id else "none",
                }
        return out
