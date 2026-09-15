"""Video status from YouTube Studio, reported by the owner's worker.

The server cannot see a private video's fate on its own. The nightly check
reads the public channel page, which never shows private videos, so a deleted
private video read "Private" forever - and one with a failed download sat in
the failure banner, retried every day. These tests pin what a Studio report is
allowed to conclude, and just as much what a report that says nothing must
leave alone.
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from app import archive, auto_download, sync_state
from app.models import (
    ChannelOwnership,
    SyncJob,
    User,
    UserChannel,
    UserChannelVideo,
    Video,
)
from app.routes import youtube as yt

CH = "UCstudio"


def _setup(db, *, owned=True):
    u = User(
        id="u1",
        username="u1",
        email="u1@example.com",
        password_hash="$2b$12$placeholder",
    )
    db.add(u)
    db.flush()
    # Alerts off: these tests are about the archive's state, and the mail path
    # is best-effort and gated separately.
    db.add(
        UserChannel(
            user_id=u.id,
            channel_id=CH,
            data_json=json.dumps({"settings": {"notifyVideoDeleted": False}}),
        )
    )
    ch = archive.ensure_channel(db, CH, title="Chan")
    if owned:
        db.add(ChannelOwnership(user_id=u.id, channel_id=ch.id, google_user_id="worker"))
    db.flush()
    return u, ch


def _row(db, u, ch, vid, *, title=None, status="archived", privacy="private", **extra):
    blob = {"id": vid, "title": title or vid, "status": status, "privacy": privacy}
    blob.update(extra)
    db.add(
        UserChannelVideo(
            user_id=u.id, channel_id=CH, video_id=vid, data_json=json.dumps(blob)
        )
    )
    archive.ensure_placeholder_video(
        db, channel=ch, youtube_video_id=vid, title=blob["title"], privacy=privacy
    )
    db.flush()


def _data(db, u, vid):
    return json.loads(db.get(UserChannelVideo, (u.id, CH, vid)).data_json)


def _pool(db, vid):
    return db.query(Video).filter(Video.youtube_id == vid).one()


def _report(db, u, videos):
    return yt.worker_video_statuses(
        payload={"channelId": CH, "videos": videos}, db=db, current=u
    )


def _failed_job(db, u, vid):
    db.add(
        SyncJob(
            user_id=u.id,
            channel_id=CH,
            video_id=vid,
            kind="video",
            status="failed",
            error=f"yt-dlp failed: ERROR: [youtube] {vid}: Video unavailable",
        )
    )
    db.flush()


# ---- what a report may conclude ---------------------------------------


def test_a_deleted_video_is_marked_gone_and_leaves_the_failure_banner(db):
    u, ch = _setup(db)
    _row(db, u, ch, "v1", status="failed")
    _failed_job(db, u, "v1")
    assert "v1" in sync_state.failed_video_ids(db, u.id)

    out = _report(db, u, [{"id": "v1", "status": "VIDEO_STATUS_DELETED"}])

    assert out["gone"] == 1
    assert _data(db, u, "v1")["status"] == "deleted_on_youtube"
    assert "v1" not in sync_state.failed_video_ids(db, u.id)


def test_a_record_without_a_status_proves_nothing(db):
    """Studio's answer for a video it will not show this identity - a made-up
    id, or another channel's private or deleted video."""
    u, ch = _setup(db)
    _row(db, u, ch, "v1")

    out = _report(db, u, [{"id": "v1", "status": None, "privacy": None, "title": None}])

    assert out["no_status"] == 1 and out["gone"] == 0
    assert _data(db, u, "v1")["status"] == "archived"


def test_the_archived_copy_survives_the_video_being_gone(db):
    u, ch = _setup(db)
    _row(db, u, ch, "v1", localPath="users/u1/videos/v1.mp4", fileSizeBytes=1024)

    _report(db, u, [{"id": "v1", "status": "VIDEO_STATUS_DELETED"}])

    d = _data(db, u, "v1")
    assert d["status"] == "deleted_on_youtube"
    assert d["localPath"] == "users/u1/videos/v1.mp4"
    assert d["fileSizeBytes"] == 1024


def test_a_video_seen_again_is_restored(db):
    u, ch = _setup(db)
    _row(db, u, ch, "v1", status="deleted_on_youtube", localPath="users/u1/videos/v1.mp4")

    out = _report(
        db, u, [{"id": "v1", "status": "VIDEO_STATUS_PROCESSED", "privacy": "VIDEO_PRIVACY_PRIVATE"}]
    )

    assert out["restored"] == 1
    assert _data(db, u, "v1")["status"] == "archived"


def test_a_placeholder_title_is_replaced_and_a_real_one_is_not(db):
    u, ch = _setup(db)
    _row(db, u, ch, "na1", title="NA")
    _row(db, u, ch, "real1", title="The real title")

    out = _report(db, u, [
        {"id": "na1", "status": "VIDEO_STATUS_PROCESSED",
         "privacy": "VIDEO_PRIVACY_PRIVATE", "title": "2026-8-25 Setting up Patreon"},
        {"id": "real1", "status": "VIDEO_STATUS_PROCESSED",
         "privacy": "VIDEO_PRIVACY_PRIVATE", "title": "Renamed since"},
    ])

    assert out["titles_fixed"] == 1
    assert _data(db, u, "na1")["title"] == "2026-8-25 Setting up Patreon"
    assert _pool(db, "na1").title == "2026-8-25 Setting up Patreon"
    # A real title only ever changes through the versioned rescan.
    assert _data(db, u, "real1")["title"] == "The real title"
    assert _pool(db, "real1").title == "The real title"


def test_a_privacy_change_reaches_what_the_archive_shows(db):
    u, ch = _setup(db)
    _row(db, u, ch, "v1", privacy="private")
    visibility_before = _pool(db, "v1").visibility

    out = _report(
        db, u, [{"id": "v1", "status": "VIDEO_STATUS_PROCESSED", "privacy": "VIDEO_PRIVACY_PUBLIC"}]
    )

    assert out["privacy_changed"] == 1
    assert _data(db, u, "v1")["privacy"] == "public"
    assert _pool(db, "v1").privacy_current == "public"
    assert _pool(db, "v1").visibility == visibility_before, "frozen at capture"


def test_only_the_owner_may_report(db):
    u, ch = _setup(db, owned=False)
    _row(db, u, ch, "v1")

    with pytest.raises(HTTPException) as exc:
        _report(db, u, [{"id": "v1", "status": "VIDEO_STATUS_DELETED"}])

    assert exc.value.status_code == 403
    assert _data(db, u, "v1")["status"] == "archived"


def test_the_worker_gets_ids_only_for_a_channel_it_owns(db):
    u, ch = _setup(db, owned=False)
    _row(db, u, ch, "v1")
    assert yt.worker_channel_video_ids(channel_id=CH, db=db, current=u) == {"videoIds": []}

    db.add(ChannelOwnership(user_id=u.id, channel_id=ch.id, google_user_id="worker"))
    db.flush()
    assert yt.worker_channel_video_ids(channel_id=CH, db=db, current=u) == {"videoIds": ["v1"]}


# ---- gone stays gone everywhere else -----------------------------------


def test_a_later_download_failure_does_not_undo_gone(db):
    u, ch = _setup(db)
    _row(db, u, ch, "v1", status="deleted_on_youtube")
    job = SyncJob(
        user_id=u.id, channel_id=CH, video_id="v1", kind="video",
        status="running", claimed_by=u.id,
    )
    db.add(job)
    db.flush()

    yt.fail_sync_job(
        job.id,
        payload={"error": "yt-dlp failed: ERROR: [youtube] v1: Video unavailable"},
        db=db,
        current=u,
    )

    assert _data(db, u, "v1")["status"] == "deleted_on_youtube"
    assert "v1" not in sync_state.failed_video_ids(db, u.id)


def test_retry_on_launch_skips_gone_videos_but_not_real_failures(db):
    u, ch = _setup(db)
    _row(db, u, ch, "gone", status="deleted_on_youtube")
    _row(db, u, ch, "flaky", status="failed")
    _failed_job(db, u, "gone")
    _failed_job(db, u, "flaky")

    assert yt.retry_failed_sync_jobs(db=db, current=u) == {"retried": 1}


def test_the_auto_download_sweep_never_queues_a_gone_video(db):
    u, ch = _setup(db)
    _row(db, u, ch, "gone", status="deleted_on_youtube")
    _row(db, u, ch, "new", status="discovered")
    uc = db.get(UserChannel, (u.id, CH))

    pending = auto_download.pending_new_uploads(db, uc)
    assert "gone" not in pending and "new" in pending

    created = auto_download.enqueue_downloads(
        db, user_id=u.id, channel_youtube_id=CH, video_ids=["gone", "new"]
    )
    assert created == 1


# ---- placeholder titles ------------------------------------------------


def test_placeholder_titles_are_recognised():
    assert archive.is_placeholder_title("NA", "vid123")
    assert archive.is_placeholder_title(" na ", "vid123")
    assert archive.is_placeholder_title("", "vid123")
    assert archive.is_placeholder_title(None, "vid123")
    assert archive.is_placeholder_title("vid123", "vid123")
    assert not archive.is_placeholder_title("Setting up Patreon", "vid123")


def test_a_sync_fills_a_placeholder_title_but_never_rewrites_a_real_one(db):
    u, ch = _setup(db)
    archive.ensure_placeholder_video(db, channel=ch, youtube_video_id="v1", title="NA")
    assert _pool(db, "v1").title == "v1", "NA is never stored as a title"

    archive.record_synced_video(
        db, user_id=u.id, youtube_channel_id=CH, youtube_video_id="v1",
        title="Setting up Patreon", privacy="private",
    )
    assert _pool(db, "v1").title == "Setting up Patreon"

    archive.record_synced_video(
        db, user_id=u.id, youtube_channel_id=CH, youtube_video_id="v1",
        title="Something else", privacy="private",
    )
    assert _pool(db, "v1").title == "Setting up Patreon"
