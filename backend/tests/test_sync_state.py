"""What counts as "this video failed to back up".

The home page banner and the video listings used to answer this
differently, which produced a banner reading "3 videos failed" above a
list that could only ever show one of them. These tests pin the single
definition both now use.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import archive, sync_state
from app.models import SyncJob, User, UserChannelVideo, Video


def _user(db, uid="u1"):
    u = User(
        id=uid,
        username=uid,
        email=f"{uid}@example.com",
        password_hash="$2b$12$placeholder",
    )
    db.add(u)
    db.flush()
    return u


def _job(db, user, vid, status, *, error=None, minutes_ago=0):
    db.add(
        SyncJob(
            user_id=user.id,
            channel_id="UCaaa",
            video_id=vid,
            kind="video",
            status=status,
            error=error,
            created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        )
    )
    db.flush()


def test_a_plain_failure_counts(db):
    u = _user(db)
    _job(db, u, "v1", "failed", error="yt-dlp failed: HTTP 403")
    assert sync_state.failed_video_ids(db, u.id) == {"v1"}


def test_an_unaired_scheduled_stream_does_not_count(db):
    """"This live event will begin in 3 hours" is not a backup failure.
    There is no file yet. Counting it tells the user something is wrong
    when nothing is, and it cannot be actioned or retried into success.
    """
    u = _user(db)
    _job(
        db,
        u,
        "v1",
        "failed",
        error="yt-dlp failed: ERROR: [youtube] v1: This live event will begin in 3 hours.",
    )
    assert sync_state.failed_video_ids(db, u.id) == set()


def test_a_stream_that_aired_and_then_really_failed_counts(db):
    """The not-yet-aired pass keys off the LATEST attempt, so a stream
    that goes live and then fails for a real reason still surfaces."""
    u = _user(db)
    _job(db, u, "v1", "failed", error="This live event will begin in 2 hours.", minutes_ago=120)
    _job(db, u, "v1", "failed", error="yt-dlp failed: HTTP 403", minutes_ago=1)
    assert sync_state.failed_video_ids(db, u.id) == {"v1"}


def test_a_video_being_retried_right_now_does_not_count(db):
    u = _user(db)
    _job(db, u, "v1", "failed", error="timed out")
    _job(db, u, "v1", "pending")
    assert sync_state.failed_video_ids(db, u.id) == set()


def test_a_video_we_already_hold_does_not_count(db):
    """Otherwise the number never goes down."""
    u = _user(db)
    _job(db, u, "v1", "failed", error="timed out")
    db.add(
        UserChannelVideo(
            user_id=u.id,
            channel_id="UCaaa",
            video_id="v1",
            data_json='{"status": "archived"}',
        )
    )
    db.flush()
    assert sync_state.failed_video_ids(db, u.id) == set()


def test_failures_are_not_forgotten_after_time_passes(db):
    """No time window on purpose: a video that failed a month ago and was
    never retried is still not backed up. Ageing it out would make the
    banner say everything is fine while a video is missing."""
    u = _user(db)
    _job(db, u, "v1", "failed", error="timed out", minutes_ago=60 * 24 * 45)
    assert sync_state.failed_video_ids(db, u.id) == {"v1"}


# ---- placeholder pool rows -------------------------------------------


def test_a_video_that_never_synced_can_still_get_a_pool_row(db):
    """Pool rows were only ever created by a successful sync or a PubSub
    notice about a public upload, so a video that failed every attempt
    existed nowhere the UI could read."""
    ch = archive.ensure_channel(db, "UCaaa", title="Alpha")
    v = archive.ensure_placeholder_video(
        db, channel=ch, youtube_video_id="v1", title="never worked",
        privacy="private",
    )
    assert v.title == "never worked"
    assert v.r2_key is None
    assert v.visibility == "sealed"


def test_an_unknown_privacy_placeholder_is_sealed_not_open(db):
    """Guessing wrong must withhold a video, never expose one."""
    ch = archive.ensure_channel(db, "UCaaa", title="Alpha")
    v = archive.ensure_placeholder_video(db, channel=ch, youtube_video_id="v1")
    assert v.visibility == "sealed"


def test_placeholder_is_not_duplicated(db):
    ch = archive.ensure_channel(db, "UCaaa", title="Alpha")
    a = archive.ensure_placeholder_video(db, channel=ch, youtube_video_id="v1")
    b = archive.ensure_placeholder_video(db, channel=ch, youtube_video_id="v1")
    assert a.id == b.id
    assert db.query(Video).filter(Video.youtube_id == "v1").count() == 1


def test_first_real_capture_restamps_a_placeholder_visibility(db):
    """A conservative guess must not outlive the guess. "Frozen at
    capture" protects a visibility decided AT capture; a placeholder
    never had one, so a public video stamped sealed while we knew
    nothing has to open up when we finally capture it - otherwise it
    stays hidden from every other subscriber forever.
    """
    ch = archive.ensure_channel(db, "UCaaa", title="Alpha")
    archive.ensure_placeholder_video(db, channel=ch, youtube_video_id="v1")

    v = archive.record_synced_video(
        db,
        user_id=_user(db).id,
        youtube_channel_id="UCaaa",
        youtube_video_id="v1",
        privacy="public",
        r2_key="users/u/videos/v1.mp4",
        bytes_stored=1024,
    )
    assert v.visibility == "open", "placeholder promoted on first capture"


def test_a_real_capture_visibility_is_still_frozen(db):
    """The promotion must not become a general "recompute visibility",
    which would undo the whole point: a video captured while public
    stays open after the creator privates it.
    """
    u = _user(db)
    archive.record_synced_video(
        db,
        user_id=u.id,
        youtube_channel_id="UCaaa",
        youtube_video_id="v1",
        privacy="public",
        r2_key="users/u/videos/v1.mp4",
        bytes_stored=1024,
    )
    v = archive.record_synced_video(
        db,
        user_id=u.id,
        youtube_channel_id="UCaaa",
        youtube_video_id="v1",
        privacy="private",
        r2_key="users/u/videos/v1.mp4",
        bytes_stored=1024,
    )
    assert v.visibility == "open", "we keep what we captured"
    assert v.privacy_current == "private"
