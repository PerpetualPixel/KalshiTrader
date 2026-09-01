"""Second factor and login throttling on the dashboard."""
import pytest

from src.auth import MAX_ATTEMPTS, AuthManager
from src.database import Database


@pytest.fixture
def auth(tmp_path):
    a = AuthManager(Database(str(tmp_path / "test.db")))
    a.set_password("correct-horse")
    return a


def test_password_alone_is_enough_without_a_pin(auth):
    assert auth.has_pin() is False
    assert auth.verify_credentials("correct-horse", None) is True


def test_pin_becomes_required_once_set(auth):
    auth.set_pin("2468")
    assert auth.has_pin() is True
    assert auth.verify_credentials("correct-horse", "2468") is True
    assert auth.verify_credentials("correct-horse", None) is False
    assert auth.verify_credentials("correct-horse", "1111") is False
    assert auth.verify_credentials("wrong", "2468") is False


def test_clearing_the_pin_restores_password_only_login(auth):
    auth.set_pin("2468")
    auth.clear_pin()
    assert auth.has_pin() is False
    assert auth.verify_credentials("correct-horse", None) is True


@pytest.mark.parametrize("bad", ["12", "1" * 13, "abcd", "12a4", ""])
def test_invalid_pins_are_rejected(auth, bad):
    with pytest.raises(ValueError):
        auth.set_pin(bad)


def test_lockout_starts_after_max_attempts(auth):
    assert auth.lockout_remaining() == 0
    for _ in range(MAX_ATTEMPTS - 1):
        auth.register_failure()
    assert auth.lockout_remaining() == 0, "must not lock out early"
    auth.register_failure()
    assert auth.lockout_remaining() > 0


def test_lockout_window_doubles_each_round(auth):
    for _ in range(MAX_ATTEMPTS):
        auth.register_failure()
    first = auth.lockout_remaining()
    for _ in range(MAX_ATTEMPTS):
        auth.register_failure()
    assert auth.lockout_remaining() >= first * 2 - 1


def test_success_clears_the_throttle(auth):
    for _ in range(MAX_ATTEMPTS):
        auth.register_failure()
    assert auth.lockout_remaining() > 0
    auth.register_success()
    assert auth.lockout_remaining() == 0


def test_a_pin_is_not_stored_in_the_clear(auth):
    auth.set_pin("2468")
    assert "2468" not in (auth.db.get_kv("auth_pin") or "")
