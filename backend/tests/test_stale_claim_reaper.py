"""The stale-claim reaper must only ever reap the caller's own claims.

Found while designing back-catalogue backfill. _reap_stale_claims filtered
on status == "running" across ALL users, and every worker calls it on each
claim poll. So the moment a second user existed, their routine poll would
revert another user's actively-running download to pending.

That is not a harmless retry. The original worker finishes, PUTs its mp4
to Backblaze on a still-valid presigned URL, and gets a 409 from
/complete - which the worker deliberately does not treat as a failure. The
object lands in the bucket with no storage_ledger row: bytes we pay for
and cannot see.

A claim is only stale from the point of view of the machine that made it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import SyncJob, User
from app.routes.youtube import HEARTBEAT_STALE_SECONDS, _reap_stale_claims


def _user(db, uid):
    u = User(id=uid, username=uid, email=f"{uid}@x.com", password_hash="p")
    db.add(u)
    db.flush()
    return u


def _running_job(db, user, *, heartbeat_age_s):
    job = SyncJob(
        user_id=user.id,
        channel_id="UCx",
        video_id=f"v-{user.id}",
        kind="video",
        status="running",
        claimed_by="worker",
        heartbeat_at=datetime.now(timezone.utc)
        - timedelta(seconds=heartbeat_age_s),
    )
    db.add(job)
    db.flush()
    return job


def test_does_not_reap_another_users_running_job(db):
    """The regression. Alice polling must not touch Bob's download."""
    alice, bob = _user(db, "alice"), _user(db, "bob")
    bobs = _running_job(db, bob, heartbeat_age_s=HEARTBEAT_STALE_SECONDS + 60)

    _reap_stale_claims(db, alice.id)
    db.flush()

    assert bobs.status == "running", (
        "one user's poll must never hand away another user's in-flight job"
    )
    assert bobs.claimed_by == "worker"


def test_reaps_the_callers_own_stale_claim(db):
    u = _user(db, "solo")
    job = _running_job(db, u, heartbeat_age_s=HEARTBEAT_STALE_SECONDS + 60)

    _reap_stale_claims(db, u.id)
    db.flush()

    assert job.status == "pending", "a genuinely abandoned claim is recoverable"
    assert job.claimed_by is None
    assert job.heartbeat_at is None


def test_leaves_a_fresh_claim_alone(db):
    """The keepalive exists so long downloads stay fresh - honour it."""
    u = _user(db, "busy")
    job = _running_job(db, u, heartbeat_age_s=30)

    _reap_stale_claims(db, u.id)
    db.flush()

    assert job.status == "running"
