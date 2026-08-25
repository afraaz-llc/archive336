"""A paused channel does not download, by any route.

The nightly sweep and the new-upload notification both consult
auto_download_enabled and honour the Active switch. POST /sync-files -
the Sync button - never checked, so it queued 498 videos onto a channel
the owner had deliberately paused: roughly 115 GB of storage he had
switched off and was about to be billed for.

There is a reading where an explicit manual sync should override a
pause. But the control is labelled "Active", and a switch that stops
some downloads and not others is not a switch anyone can reason about.
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from app import archive
from app.models import (
    User,
    UserChannel,
    UserChannelSubscription,
    UserChannelVideo,
)
from app.routes.youtube import enqueue_sync_files


def _setup(db, *, active: bool):
    u = User(
        id="u1", username="u1", email="u1@x.com",
        password_hash="p", payment_status="active",
    )
    db.add(u)
    ch = archive.ensure_channel(db, "UCx", title="X")
    sub = UserChannelSubscription(user_id=u.id, channel_id=ch.id)
    sub.settings_json = json.dumps({"active": active})
    db.add(sub)
    db.add(UserChannel(
        user_id=u.id, channel_id="UCx", google_user_id=None,
        data_json=json.dumps({
            "id": "UCx", "name": "X", "settings": {"active": active},
        }),
    ))
    db.add(UserChannelVideo(
        user_id=u.id, channel_id="UCx", video_id="v1",
        data_json=json.dumps({"status": "discovered"}),
    ))
    db.flush()
    return u


def test_a_paused_channel_refuses_to_sync(db):
    u = _setup(db, active=False)

    with pytest.raises(HTTPException) as e:
        enqueue_sync_files(
            channel_id="UCx", payload={"video_ids": ["v1"]}, db=db, current=u,
        )

    assert e.value.status_code == 409
    assert "paused" in e.value.detail.lower()


def test_an_active_channel_still_syncs(db):
    """The gate must not become a wall - this is the normal path."""
    u = _setup(db, active=True)

    out = enqueue_sync_files(
        channel_id="UCx", payload={"video_ids": ["v1"]}, db=db, current=u,
    )

    assert out["enqueued"] == 1


def test_the_subscription_row_decides(db):
    """Settings live in two rows and every read is served from the
    subscription. A stale legacy copy saying "active" must not let a
    paused channel through the gate."""
    u = _setup(db, active=True)
    sub = db.query(UserChannelSubscription).one()
    sub.settings_json = json.dumps({"active": False})
    db.flush()

    with pytest.raises(HTTPException) as e:
        enqueue_sync_files(
            channel_id="UCx", payload={"video_ids": ["v1"]}, db=db, current=u,
        )
    assert e.value.status_code == 409


def test_captions_also_refuse_while_paused(db):
    """Captions are stored bytes too, so they are spending."""
    from app.routes.youtube import enqueue_sync_captions

    u = _setup(db, active=False)
    with pytest.raises(HTTPException) as e:
        enqueue_sync_captions(channel_id="UCx", db=db, current=u)
    assert e.value.status_code == 409


def test_metadata_jobs_refuse_while_paused(db):
    from app.routes.youtube import enqueue_metadata_jobs

    u = _setup(db, active=False)
    with pytest.raises(HTTPException) as e:
        enqueue_metadata_jobs(db, user_id=u.id, channel_id="UCx")
    assert e.value.status_code == 409


def test_comment_jobs_refuse_while_paused(db):
    from app.routes.youtube import enqueue_comment_jobs

    u = _setup(db, active=False)
    with pytest.raises(HTTPException) as e:
        enqueue_comment_jobs(db, user_id=u.id, channel_id="UCx")
    assert e.value.status_code == 409


def test_an_untracked_channel_is_left_to_its_own_404(db):
    """The gate must not turn "you don't have this channel" into
    "it's paused" - that would be a worse lie than the one it fixes."""
    from app.routes.youtube import _require_channel_active

    u = _setup(db, active=False)
    _require_channel_active(db, u.id, "UCsomeoneelse")  # must not raise


def test_a_paused_channel_hands_out_no_queued_work(db):
    """The other half of the pause, and the one that made it look broken.

    Gating the enqueue endpoints stopped NEW jobs but did nothing about
    the 405 already queued, so the worker kept draining them after the
    channel was switched off. Skipped, not cancelled: the jobs stay
    pending so turning the channel back on resumes where it stopped.
    """
    from app.models import SyncJob
    from app.routes.youtube import _next_claimable_job

    u = _setup(db, active=True)
    db.add(SyncJob(
        user_id=u.id, channel_id="UCx", video_id="v1",
        kind="video", status="pending",
    ))
    db.flush()
    assert _next_claimable_job(db, u.id) is not None, "active channel works"

    sub = db.query(UserChannelSubscription).one()
    sub.settings_json = json.dumps({"active": False})
    db.flush()

    assert _next_claimable_job(db, u.id) is None, "paused hands out nothing"

    # And the job survives, so un-pausing resumes rather than restarts.
    assert db.query(SyncJob).filter(SyncJob.status == "pending").count() == 1


def test_another_channel_keeps_working_while_one_is_paused(db):
    """Pausing one channel must not stop the others."""
    from app.models import SyncJob
    from app.routes.youtube import _next_claimable_job

    u = _setup(db, active=False)
    other = archive.ensure_channel(db, "UCother", title="Other")
    sub2 = UserChannelSubscription(user_id=u.id, channel_id=other.id)
    sub2.settings_json = json.dumps({"active": True})
    db.add(sub2)
    db.add(SyncJob(
        user_id=u.id, channel_id="UCx", video_id="paused-one",
        kind="video", status="pending",
    ))
    db.add(SyncJob(
        user_id=u.id, channel_id="UCother", video_id="live-one",
        kind="video", status="pending",
    ))
    db.flush()

    job = _next_claimable_job(db, u.id)
    assert job is not None and job.video_id == "live-one"
