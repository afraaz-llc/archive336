"""Throttling on the endpoints anyone can reach without logging in.

The one that mattered was password reset. It is otherwise careful -
always 204 so nobody can probe which addresses exist, tokens stored only
as hashes - but nothing capped how often it would send, so anyone could
POST a known address in a loop and have us mail that person a reset link
every time. An email bomb, sent by us, on our sending domain.
"""
from __future__ import annotations

import pytest

from app import rate_limit


@pytest.fixture(autouse=True)
def _clean():
    rate_limit.reset()
    yield
    rate_limit.reset()


def test_allows_up_to_the_limit_then_refuses():
    for i in range(3):
        assert rate_limit.hit("b", "k", limit=3, window_seconds=60) == 0, i
    assert rate_limit.hit("b", "k", limit=3, window_seconds=60) > 0


def test_a_refused_attempt_does_not_extend_its_own_lockout():
    """Otherwise hammering a limited key holds it locked forever and the
    window never drains - the attacker would be choosing the duration."""
    for _ in range(3):
        rate_limit.hit("b", "k", limit=3, window_seconds=60)

    first = rate_limit.hit("b", "k", limit=3, window_seconds=60)
    for _ in range(20):
        rate_limit.hit("b", "k", limit=3, window_seconds=60)
    later = rate_limit.hit("b", "k", limit=3, window_seconds=60)

    assert later <= first, "the wait counts down rather than resetting"


def test_keys_do_not_share_a_budget():
    for _ in range(3):
        rate_limit.hit("b", "alice", limit=3, window_seconds=60)
    assert rate_limit.hit("b", "bob", limit=3, window_seconds=60) == 0


def test_buckets_do_not_share_a_budget():
    """Signing in badly must not use up someone's password resets."""
    for _ in range(3):
        rate_limit.hit("login-fail", "1.2.3.4", limit=3, window_seconds=60)
    assert rate_limit.hit("reset-ip", "1.2.3.4", limit=3, window_seconds=60) == 0


def test_an_empty_key_fails_open():
    """No CF header and no client address should not throttle everyone
    into one anonymous bucket - a missing header would become a
    site-wide outage."""
    for _ in range(50):
        assert rate_limit.hit("b", "", limit=3, window_seconds=60) == 0


def test_the_window_drains(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(rate_limit.time, "time", lambda: now[0])

    for _ in range(3):
        rate_limit.hit("b", "k", limit=3, window_seconds=60)
    assert rate_limit.hit("b", "k", limit=3, window_seconds=60) > 0

    now[0] += 61
    assert rate_limit.hit("b", "k", limit=3, window_seconds=60) == 0


def test_state_is_swept_rather_than_growing_without_bound(monkeypatch):
    """Someone cycling addresses purely to grow the table should not be
    able to. Bounded memory, not just a bounded window."""
    now = [1000.0]
    monkeypatch.setattr(rate_limit.time, "time", lambda: now[0])
    monkeypatch.setattr(rate_limit, "_SWEEP_THRESHOLD", 50)

    for i in range(60):
        rate_limit.hit("b", f"key-{i}", limit=3, window_seconds=60)
    assert rate_limit.tracked_keys() > 0

    now[0] += 4000  # past the sweep horizon
    rate_limit.hit("b", "fresh", limit=3, window_seconds=60)
    assert rate_limit.tracked_keys() < 60, "stale keys were dropped"


def test_the_configured_limits_are_sane():
    """A limit low enough to catch a real person is worse than none -
    they cannot tell it from the product being broken."""
    for name, (limit, window) in (
        ("signup", rate_limit.SIGNUP_PER_IP),
        ("login", rate_limit.LOGIN_FAILURES_PER_IP),
        ("reset/email", rate_limit.RESET_PER_EMAIL),
        ("reset/ip", rate_limit.RESET_PER_IP),
    ):
        assert limit >= 3, f"{name} is tight enough to hit by accident"
        assert window <= 3600, f"{name} locks someone out for over an hour"
