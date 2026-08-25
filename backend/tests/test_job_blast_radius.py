"""Bounds on how much work a queue can keep doing after the user stops it.

Written before the back-catalogue backfill, on purpose: both of these are
no-ops against today's near-empty queue and become the difference between
a bill and a runaway once a single add can queue 20,000 videos.

Nothing in the codebase cancelled a SyncJob. Channel removal soft-deleted
the subscription and touched sync_jobs not at all, and the claim query
filters on user_id and status only - so a removed channel, or one whose
owner's card had lapsed, kept downloading and kept generating billed
storage until the queue drained on its own.
"""
from __future__ import annotations

from datetime import datetime, timezone

import json

from app.models import SyncJob, User, UserChannel
from app.routes.youtube import (
    _next_claimable_job,
    claim_sync_job,
    delete_channel,
)
from app.service_access import service_is_active


def _user(db, uid, payment_status="active"):
    u = User(
        id=uid, username=uid, email=f"{uid}@x.com",
        password_hash="p", payment_status=payment_status,
    )
    db.add(u)
    db.flush()
    return u


def _track(db, user, channel="UCx"):
    """delete_channel 404s on a channel it has never heard of."""
    row = UserChannel(
        user_id=user.id, channel_id=channel, google_user_id=None,
        data_json=json.dumps({"id": channel}),
    )
    db.add(row)
    db.flush()
    return row


def _job(db, user, *, status="pending", channel="UCx", vid="v1"):
    j = SyncJob(
        user_id=user.id, channel_id=channel, video_id=vid,
        kind="video", status=status,
        claimed_by="worker" if status == "running" else None,
        heartbeat_at=datetime.now(timezone.utc) if status == "running" else None,
    )
    db.add(j)
    db.flush()
    return j


def test_removing_a_channel_cancels_its_pending_jobs(db):
    u = _user(db, "u-rm")
    _track(db, u)
    queued = _job(db, u, vid="v-queued")

    delete_channel(channel_id="UCx", db=db, current=u)
    db.flush()

    assert queued.status == "failed", (
        "a cancelled channel must stop downloading onto the user's bill"
    )
    assert queued.error == "cancelled: channel removed"


def test_a_running_job_is_left_to_finish(db):
    """Abandoning a job mid-flight is how an object lands in Backblaze
    with no ledger row - bytes we pay for and cannot see."""
    u = _user(db, "u-rm2")
    _track(db, u)
    running = _job(db, u, status="running", vid="v-running")

    delete_channel(channel_id="UCx", db=db, current=u)
    db.flush()

    assert running.status == "running"


def test_other_channels_are_untouched(db):
    u = _user(db, "u-rm3")
    _track(db, u)
    keep = _job(db, u, channel="UCother", vid="v-keep")

    delete_channel(channel_id="UCx", db=db, current=u)
    db.flush()

    assert keep.status == "pending"


def test_paused_account_is_handed_no_new_work(db):
    """A lapsed card stops the NEXT job, not the current one."""
    u = _user(db, "u-paused", payment_status="past_due")
    _job(db, u, vid="v-waiting")

    assert claim_sync_job(db=db, current=u, session_token=None) is None


def test_active_account_still_gets_work(db):
    """Checked below the endpoint: claim_sync_job also mints a presigned
    upload URL, which needs live storage credentials. The guardrail being
    tested is the entitlement gate and the job being claimable."""
    u = _user(db, "u-ok")
    _job(db, u, vid="v-ready")

    assert service_is_active(u) is True
    assert _next_claimable_job(db, u.id) is not None, (
        "a paying user's queued job must still be handed out"
    )
