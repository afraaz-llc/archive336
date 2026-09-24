"""A fresh upload YouTube has not finished processing is not a failure.

The owner uploaded a video at 20:37 and its card showed a red "Failed"
for the rest of the night. yt-dlp had returned YouTube's own answer -
"We're processing this video. Check back later." - five times in two
hours, which is exactly what the fast retry burst is for. But every one
of those counted against the video, so it spent the burst before the
transcode finished and dropped to one attempt a day. The file was ready
within hours; the backup was not scheduled to try again until the next
night, and the card said the backup had failed the whole time.

Both halves are wrong for the same reason: "not ready yet" is not a
verdict on the video.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app import archive, auto_download
from app.models import (
    SyncJob,
    User,
    UserChannel,
    UserChannelSubscription,
    Video,
)
from app.routes.youtube import _friendly_sync_error

PROCESSING = (
    "yt-dlp failed: ERROR: [youtube] C2RmDzImtAA: "
    "We're processing this video. Check back later."
)


def test_processing_says_nothing_about_the_video(db):
    assert auto_download.failure_counts_against_video(PROCESSING) is False


def test_a_real_verdict_still_counts(db):
    """Guard: the marker must not have widened into a blanket excuse."""
    assert auto_download.failure_counts_against_video(
        "ERROR: [youtube] x: This video is private"
    ) is True


def test_the_card_explains_the_wait(db):
    assert _friendly_sync_error(PROCESSING) == (
        "YouTube is still processing it - we will retry"
    )


def _setup(db):
    u = User(
        id="u1", username="u1", email="u1@x.com",
        password_hash="p", payment_status="active",
    )
    db.add(u)
    db.flush()
    ch = archive.ensure_channel(db, "UCme")
    db.add(UserChannelSubscription(user_id=u.id, channel_id=ch.id))
    uc = UserChannel(
        user_id=u.id, channel_id="UCme", google_user_id=None,
        data_json=json.dumps({
            "id": "UCme",
            "addedAt": datetime.now(timezone.utc).isoformat(),
            "settings": {"active": True, "downloadNewVideos": True},
        }),
    )
    db.add(uc)
    db.add(Video(
        channel_id=ch.id, youtube_id="v1", title="v1",
        published_at=datetime.now(timezone.utc) - timedelta(days=1),
        privacy_at_discovery="public", privacy_current="public",
        visibility="open", r2_key=None,
    ))
    db.flush()
    return u, uc


def _failure(db, user, error, *, minutes_ago):
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    db.add(SyncJob(
        user_id=user.id, channel_id="UCme", video_id="v1", kind="video",
        status="failed", error=error, created_at=when, finished_at=when,
    ))
    db.flush()


def _sweep(db, user):
    """One pass of the half-hourly sweep. Returns jobs created."""
    return auto_download.enqueue_downloads(
        db, user_id=user.id, channel_youtube_id="UCme", video_ids=["v1"]
    )


def test_the_burst_survives_a_run_of_processing_failures(db):
    """Five "still processing" answers in two hours, which is what the
    owner's upload produced. The next sweep must still try again: the
    whole point of retrying fast is that this resolves on its own."""
    u, uc = _setup(db)
    for i in range(5):
        _failure(db, u, PROCESSING, minutes_ago=120 - i * 25)

    assert _sweep(db, u) == 1


def test_five_real_failures_still_back_off(db):
    """Guard: the backoff still exists for failures that mean something,
    so a private video is not retried every half hour forever."""
    u, uc = _setup(db)
    for i in range(5):
        _failure(db, u, "ERROR: This video is private", minutes_ago=120 - i * 25)

    assert _sweep(db, u) == 0
