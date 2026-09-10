"""The channel page's video list: GET /api/youtube/channels/{id}/videos.

It has to call a video failed exactly when the Home banner and the library
do. It used to report each row's raw status, so a scheduled stream that has
not aired read "Failed" on the channel page after the banner had already
forgiven it - Le Frog's page showed three failures where there were two.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app import archive
from app.models import SyncJob, User, UserChannelSubscription, UserChannelVideo, Video
from app.routes.youtube import list_channel_videos


def _setup(db, *, error=None, blob=None):
    u = User(
        id="u1",
        username="u1",
        email="u1@example.com",
        password_hash="$2b$12$placeholder",
    )
    db.add(u)
    db.flush()
    ch = archive.ensure_channel(db, "UCaaa", title="Alpha")
    db.add(UserChannelSubscription(user_id=u.id, channel_id=ch.id))
    db.add(
        Video(
            channel_id=ch.id,
            youtube_id="v1",
            title="one",
            published_at=datetime.now(timezone.utc),
            privacy_at_discovery="public",
            privacy_current="public",
            visibility="open",
            r2_key=None,
            bytes_stored=None,
        )
    )
    if error is not None:
        db.add(
            SyncJob(
                user_id=u.id, channel_id=ch.youtube_id, video_id="v1",
                kind="video", status="failed", error=error,
            )
        )
    if blob is not None:
        db.add(
            UserChannelVideo(
                user_id=u.id, channel_id=ch.youtube_id, video_id="v1", data_json=blob,
            )
        )
    db.flush()
    return u


def _item(db, u):
    items = list_channel_videos(
        channel_id="UCaaa", cursor=None, limit=50, db=db, current=u
    )["items"]
    assert len(items) == 1, "the fixture's one video is listed"
    return items[0]


def test_an_unaired_scheduled_stream_is_not_called_failed(db):
    u = _setup(
        db,
        error="yt-dlp failed: ERROR: [youtube] v1: This live event will begin in a few moments.",
        blob='{"status": "failed", "lastError": "Scheduled livestream - it has not aired yet"}',
    )
    assert _item(db, u)["status"] == "discovered", "known, not held, not a failure"


def test_a_real_failure_still_reads_as_failed_with_its_reason(db):
    u = _setup(
        db,
        error="yt-dlp failed: ERROR: [youtube] v1: Video unavailable",
        blob='{"status": "failed", "lastError": "Unavailable on YouTube"}',
    )
    item = _item(db, u)
    assert item["status"] == "failed"
    assert item["lastError"] == "Unavailable on YouTube", "the card can say why"


def test_a_failure_with_no_row_of_its_own_still_reads_as_failed(db):
    """A video whose first attempt failed has no per-user row to say so.
    The library already counts it; the channel page has to agree."""
    u = _setup(db, error="yt-dlp failed: ERROR: [youtube] v1: Video unavailable")
    assert _item(db, u)["status"] == "failed"
