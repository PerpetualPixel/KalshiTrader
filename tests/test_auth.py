import pytest

from src.auth import AuthManager
from src.database import Database


@pytest.fixture()
def auth(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    return AuthManager(db)


def test_setup_and_verify_password(auth):
    assert not auth.is_configured()
    auth.set_password("hunter2hunter2")
    assert auth.is_configured()
    assert auth.verify_password("hunter2hunter2")
    assert not auth.verify_password("wrong-password")


def test_short_password_rejected(auth):
    with pytest.raises(ValueError):
        auth.set_password("short")


def test_token_roundtrip(auth):
    token = auth.issue_token()
    assert auth.verify_token(token)
    assert not auth.verify_token(None)
    assert not auth.verify_token("garbage")
    assert not auth.verify_token(token + "x")


def test_expired_token_rejected(auth):
    import hashlib
    import hmac
    import time

    past = str(int(time.time()) - 10)
    sig = hmac.new(auth._secret, past.encode(), hashlib.sha256).hexdigest()
    assert not auth.verify_token(f"{past}.{sig}")


def test_revoke_all_sessions(auth):
    token = auth.issue_token()
    auth.revoke_all_sessions()
    assert not auth.verify_token(token)
    assert auth.verify_token(auth.issue_token())


def test_secret_persists_across_instances(tmp_path):
    db_path = str(tmp_path / "persist.db")
    token = AuthManager(Database(db_path)).issue_token()
    # a new process/instance reading the same DB must accept the token
    assert AuthManager(Database(db_path)).verify_token(token)
