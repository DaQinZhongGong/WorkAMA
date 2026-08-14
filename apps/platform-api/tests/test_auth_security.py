from datetime import UTC, datetime, timedelta

from workama_platform.modules.auth.service import (
    auth_token_is_usable,
    next_login_failure,
    totp_code,
    verify_totp,
)


def test_auth_token_must_be_unconsumed_and_unexpired():
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    assert auth_token_is_usable(now + timedelta(minutes=1), None, now)
    assert not auth_token_is_usable(now - timedelta(seconds=1), None, now)
    assert not auth_token_is_usable(now + timedelta(minutes=1), now, now)


def test_fifth_login_failure_locks_account_for_fifteen_minutes():
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    assert next_login_failure(3, now) == (4, None)
    failures, locked_until = next_login_failure(4, now)
    assert failures == 5
    assert locked_until == now + timedelta(minutes=15)


def test_totp_accepts_current_and_adjacent_window_only():
    secret = "JBSWY3DPEHPK3PXP"
    now = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    assert verify_totp(secret, totp_code(secret, now), now)
    assert verify_totp(secret, totp_code(secret, now - timedelta(seconds=30)), now)
    assert not verify_totp(secret, totp_code(secret, now - timedelta(seconds=90)), now)
    assert not verify_totp(secret, "000000", now)
