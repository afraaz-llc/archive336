"""What "N videos failed to back up" is allowed to mean.

The dashboard read "24 videos failed to back up" directly above
"ARCHIVED 11 / 11". Every video was safely stored; the alarm was
counting failed SyncJob ROWS from the last 24 hours, which differs from
"videos not backed up" in three ways at once. It counted every attempt
rather than every video, it counted metadata and comment jobs as videos,
and it never asked whether the video succeeded on a later try.

An alarm that fires when nothing is wrong is worse than no alarm, so
these pin the only definition worth showing a user: no file, nothing
queued to make one, and a failure behind it.
"""
from __future__ import annotations

import json

from app.models import SyncJob, User, UserChannel, UserChannelVideo
from app.routes.youtube import worker_status


def _user(db):
    u = User(
        id="u1", username="u1", email="u1@x.com",
        password_hash="p", payment_status="active",
    )
    db.add(u)
    db.add(UserChannel(
        user_id="u1", channel_id="UCx", google_user_id=None,
        data_json=json.dumps({"id": "UCx", "name": "X"}),
    ))
    db.flush()
    return u


def _job(db, vid, status, *, kind="video", error="boom"):
    db.add(SyncJob(
        user_id="u1", channel_id="UCx", video_id=vid,
        kind=kind, status=status,
        error=error if status == "failed" else None,
    ))
    db.flush()


def _archived(db, vid):
    db.add(UserChannelVideo(
        user_id="u1", channel_id="UCx", video_id=vid,
        data_json=json.dumps({"status": "archived"}),
    ))
    db.flush()


def _failed_count(db, u):
    return worker_status(db=db, current=u)["failedJobs"]


def test_a_video_that_later_succeeded_is_not_a_failure(db):
    """The actual bug. Five failed attempts then an archived file is a
    success story, not five failures."""
    u = _user(db)
    for _ in range(5):
        _job(db, "v1", "failed")
    _archived(db, "v1")

    assert _failed_count(db, u) == 0


def test_many_attempts_on_one_video_count_once(db):
    """It is a count of videos, not of attempts."""
    u = _user(db)
    for _ in range(9):
        _job(db, "v1", "failed")

    assert _failed_count(db, u) == 1


def test_a_video_queued_for_retry_is_not_reported(db):
    """Being retried right now is progress, not failure. Reporting it
    contradicts the card's own "retried automatically" line."""
    u = _user(db)
    _job(db, "v1", "failed")
    _job(db, "v1", "pending")

    assert _failed_count(db, u) == 0


def test_metadata_and_comment_failures_are_not_videos(db):
    """The card says "videos". A failed comment fetch is not one."""
    u = _user(db)
    _job(db, "v1", "failed", kind="metadata")
    _job(db, "v2", "failed", kind="comments")

    assert _failed_count(db, u) == 0


def test_a_genuinely_stuck_video_is_still_reported(db):
    """The alarm has to keep working. No file, nothing queued, a failure
    behind it - that is exactly the case worth showing."""
    u = _user(db)
    _job(db, "v1", "failed")
    _job(db, "v2", "failed")
    _archived(db, "v2")

    assert _failed_count(db, u) == 1


def test_an_old_failure_does_not_age_out(db):
    """The previous version only looked back 24 hours, so a video stuck
    since last week silently stopped being reported. It is still not
    backed up."""
    u = _user(db)
    _job(db, "v1", "failed")
    row = db.query(SyncJob).one()
    from datetime import datetime, timedelta, timezone
    row.created_at = datetime.now(timezone.utc) - timedelta(days=30)
    db.flush()

    assert _failed_count(db, u) == 1
