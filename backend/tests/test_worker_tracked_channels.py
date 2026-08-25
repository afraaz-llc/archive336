"""The worker's channel list, and why the website is its only source.

The owner's rule: "the worker app should only show channels that are being
tracked, it should get them from the website, and then it should let us
authenticate any of those channels."

Before this, the worker discovered channels from whatever Google account you
signed into, which allowed a state where you "connected" a channel the
website had never heard of. That did nothing at all - PUT /worker-connection
drops ids the shared Channel pool does not know - so the app went all-green
while backing up nothing. These tests pin the inversion: tracked-on-website
is necessary AND sufficient to appear here, and auth state is reported
per-channel rather than per-account.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app import archive
from app.models import ChannelOwnership, User, UserChannel
from app.routes.youtube import worker_tracked_channels


def _user(db, uid):
    u = User(
        id=uid,
        username=uid,
        email=f"{uid}@example.com",
        password_hash="$2b$12$placeholder",
    )
    db.add(u)
    db.flush()
    return u


def _track(db, user, youtube_id, name="A Channel", removed=False):
    """Track a channel the way the website does."""
    row = UserChannel(
        user_id=user.id,
        channel_id=youtube_id,
        google_user_id=None,
        data_json=json.dumps(
            {
                "id": youtube_id,
                "name": name,
                "handle": f"@{name.lower().replace(' ', '')}",
                "avatarUrl": "https://example.test/a.jpg",
            }
        ),
        removed_at=datetime.now(timezone.utc) if removed else None,
    )
    db.add(row)
    db.flush()
    return row


def _authenticate(db, user, youtube_id, revoked=False, user_revoked=False):
    ch = archive.ensure_channel(db, youtube_id)
    now = datetime.now(timezone.utc)
    own = ChannelOwnership(
        channel_id=ch.id,
        user_id=user.id,
        google_user_id="worker",
        revoked_at=now if (revoked or user_revoked) else None,
        user_revoked_at=now if user_revoked else None,
    )
    db.add(own)
    db.flush()
    return own


def test_tracked_channel_appears_unauthenticated(db):
    """The state the owner asked to make reachable: added on the website,
    waiting for the worker to authenticate it."""
    u = _user(db, "u-track")
    _track(db, u, "UCtracked", name="My Channel")

    out = worker_tracked_channels(db=db, current=u)

    assert len(out["channels"]) == 1
    ch = out["channels"][0]
    assert ch["youtubeId"] == "UCtracked"
    assert ch["title"] == "My Channel"
    assert ch["handle"] == "@mychannel"
    assert ch["authenticated"] is False
    assert ch["revoked"] is False


def test_authenticated_channel_reports_per_channel(db):
    u = _user(db, "u-auth")
    _track(db, u, "UCa", name="Alpha")
    _track(db, u, "UCb", name="Beta")
    _authenticate(db, u, "UCa")

    by_id = {c["youtubeId"]: c for c in worker_tracked_channels(db=db, current=u)["channels"]}

    assert by_id["UCa"]["authenticated"] is True
    assert by_id["UCb"]["authenticated"] is False, (
        "auth is per-channel - signing in for one must not vouch for another"
    )


def test_channel_owned_but_not_tracked_is_invisible(db):
    """The bug this endpoint exists to kill.

    Ownership alone must never surface a channel. Otherwise the worker
    shows something the website does not bill for, which is exactly the
    silent no-op the owner asked to make impossible.
    """
    u = _user(db, "u-owned-only")
    _authenticate(db, u, "UConlyowned")

    assert worker_tracked_channels(db=db, current=u)["channels"] == []


def test_removed_channel_is_invisible(db):
    """A channel in the 30-day grace window is not being backed up, so the
    worker must not offer to authenticate it."""
    u = _user(db, "u-removed")
    _track(db, u, "UCgone", removed=True)

    assert worker_tracked_channels(db=db, current=u)["channels"] == []


def test_revoked_is_distinct_from_never_authenticated(db):
    """The worker must tell these apart: revoked means drop the stored
    login, never-authenticated means merely offer to sign in."""
    u = _user(db, "u-revoked")
    _track(db, u, "UCrevoked")
    _authenticate(db, u, "UCrevoked", user_revoked=True)

    ch = worker_tracked_channels(db=db, current=u)["channels"][0]
    assert ch["authenticated"] is False
    assert ch["revoked"] is True


def test_another_users_tracking_does_not_leak(db):
    """The Channel pool is shared, which is what made the old behaviour
    non-deterministic. Tracking is per-user and must stay that way."""
    alice = _user(db, "u-alice")
    bob = _user(db, "u-bob")
    _track(db, alice, "UCshared", name="Shared")
    _authenticate(db, bob, "UCshared")

    assert worker_tracked_channels(db=db, current=bob)["channels"] == []
    assert len(worker_tracked_channels(db=db, current=alice)["channels"]) == 1
