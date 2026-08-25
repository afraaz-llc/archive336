"""The worker's sign-out confirmation, and what it may and may not clear.

The simpler model the owner chose: the website's "disconnected" state is
transient, collapsing to the plain connect state once the worker confirms
it finished signing out. That confirmation is this endpoint. The contract:

  - it clears the sticky user_revoked_at, and ONLY for the calling user,
  - it leaves revoked_at set - authorization returns only when a real
    sign-in report re-proves ownership,
  - unknown channels and other users' rows are untouched,
  - it is idempotent.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app import archive
from app.models import ChannelOwnership, User
from app.routes.youtube import acknowledge_revocations


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


def _revoked_ownership(db, user, youtube_id):
    ch = archive.ensure_channel(db, youtube_id)
    now = datetime.now(timezone.utc)
    own = ChannelOwnership(
        channel_id=ch.id,
        user_id=user.id,
        google_user_id="worker",
        revoked_at=now,
        user_revoked_at=now,
    )
    db.add(own)
    db.flush()
    return own


def test_ack_clears_sticky_flag_but_not_authorization(db):
    u = _user(db, "u-ack")
    own = _revoked_ownership(db, u, "UCack")

    out = acknowledge_revocations(
        payload={"channels": ["UCack"]}, db=db, current=u
    )
    assert out["cleared"] == ["UCack"]
    db.flush()
    assert own.user_revoked_at is None, "the block is lifted"
    assert own.revoked_at is not None, (
        "still not authorized - only a real sign-in report may restore that"
    )


def test_ack_only_touches_the_calling_user(db):
    alice = _user(db, "u-alice")
    bob = _user(db, "u-bob")
    own_alice = _revoked_ownership(db, alice, "UCshared")
    own_bob = ChannelOwnership(
        channel_id=own_alice.channel_id,
        user_id=bob.id,
        google_user_id="worker",
        revoked_at=datetime.now(timezone.utc),
        user_revoked_at=datetime.now(timezone.utc),
    )
    db.add(own_bob)
    db.flush()

    acknowledge_revocations(payload={"channels": ["UCshared"]}, db=db, current=alice)
    db.flush()
    assert own_alice.user_revoked_at is None
    assert own_bob.user_revoked_at is not None, "another owner's block survives"


def test_ack_is_idempotent_and_ignores_junk(db):
    u = _user(db, "u-idem")
    _revoked_ownership(db, u, "UCidem")

    first = acknowledge_revocations(
        payload={"channels": ["UCidem", "UCnotachannel", "", 42]}, db=db, current=u
    )
    assert first["cleared"] == ["UCidem"]
    second = acknowledge_revocations(
        payload={"channels": ["UCidem"]}, db=db, current=u
    )
    assert second["cleared"] == []


def test_ack_then_worker_report_reauthorizes(db):
    """The end-to-end point of the model: after the ack, a genuine sign-in
    report is enough to re-authorize - no website step needed."""
    u = _user(db, "u-flow")
    own = _revoked_ownership(db, u, "UCflow")

    acknowledge_revocations(payload={"channels": ["UCflow"]}, db=db, current=u)
    db.flush()

    # What report_worker_connection does for a signed-in owned channel.
    archive.ensure_ownership(
        db, user_id=u.id, channel_id=own.channel_id, google_user_id="worker"
    )
    db.flush()
    assert own.revoked_at is None, "sign-in re-authorizes once the block is gone"


def test_without_ack_a_worker_report_cannot_reauthorize(db):
    """The inverse guard still holds: while the block stands, machine
    chatter cannot undo a disconnect."""
    u = _user(db, "u-guard")
    own = _revoked_ownership(db, u, "UCguard")

    archive.ensure_ownership(
        db, user_id=u.id, channel_id=own.channel_id, google_user_id="worker"
    )
    db.flush()
    assert own.revoked_at is not None
    assert own.user_revoked_at is not None
