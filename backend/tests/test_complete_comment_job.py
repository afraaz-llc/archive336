"""End-to-end wiring of a completed comment job.

_complete_comment_job normalises the worker's payload, resolves the channel
owner, computes allow_deletions from the safety guards, sizes the debounce
from the channel cadence, and hands the lot to the shared store engine
(app.comments_rescan.apply_comment_snapshot). These tests drive it against a
real in-memory DB so the guards are exercised through the actual engine, not a
stub.

The load-bearing property under test is the safety rule: a comment is only ever
soft-deleted on a fetch the worker certified complete AND that clears the sanity
ratio AND was already missing a cadence ago. Anything short of all three is
insert/update-only, because a false "your comment was deleted" is the worst
output this feature can produce.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.models import SyncJob, User, UserChannel, UserChannelVideo, VideoComment
from app.routes.youtube import _complete_comment_job


USER_ID = "u-owner"
CHANNEL_ID = "UCowner"
VIDEO_ID = "vid123"


def _seed(db, *, with_video=True):
    db.add(
        User(
            id=USER_ID,
            username="owner",
            email="owner@example.com",
            password_hash="$2b$12$placeholder",
        )
    )
    db.flush()
    db.add(
        UserChannel(
            user_id=USER_ID,
            channel_id=CHANNEL_ID,
            data_json=json.dumps({"settings": {}}),
        )
    )
    db.flush()
    if with_video:
        db.add(
            UserChannelVideo(
                user_id=USER_ID,
                channel_id=CHANNEL_ID,
                video_id=VIDEO_ID,
                data_json=json.dumps({"status": "archived"}),
            )
        )
        db.flush()


def _job(db):
    job = SyncJob(
        user_id=USER_ID,
        channel_id=CHANNEL_ID,
        video_id=VIDEO_ID,
        kind="comments",
        status="running",
        claimed_by=USER_ID,
    )
    db.add(job)
    db.flush()
    return job


def _pi(**over):
    """One camelCase payload item as the worker sends it."""
    base = {
        "id": "c1",
        "parentId": None,
        "author": "Someone",
        "authorChannelId": "UCsomeoneelse",
        "text": "a comment",
        "likeCount": 0,
        "isEdited": False,
        "viewerRatingLike": False,
        "publishedAt": "2026-07-20T10:00:00Z",
        "updatedAt": None,
    }
    base.update(over)
    return base


def _existing(db, cid, *, last_seen_days_ago, deleted_at=None):
    now = datetime.now(timezone.utc)
    db.add(
        VideoComment(
            id=cid,
            user_id=USER_ID,
            channel_id=CHANNEL_ID,
            video_id=VIDEO_ID,
            author="Old",
            text="old body",
            text_hash="oldhash",
            first_seen_at=now - timedelta(days=last_seen_days_ago),
            last_seen_at=now - timedelta(days=last_seen_days_ago),
            deleted_at=deleted_at,
        )
    )
    db.flush()


def test_complete_inserts_comments_marks_job_done_and_stamps_clock(db):
    _seed(db)
    job = _job(db)
    payload = {
        "comments": {
            "complete": True,
            "reportedTotal": 2,
            "items": [
                # authorChannelId == the channel owner -> is_by_uploader
                _pi(id="top", parentId=None, authorChannelId=CHANNEL_ID),
                _pi(id="top.r1", parentId="top", authorChannelId="UCother"),
            ],
        }
    }
    now = datetime.now(timezone.utc)

    out = _complete_comment_job(db, job=job, payload=payload, now=now)

    assert out["kind"] == "comments"
    assert out["allowDeletions"] is True
    assert out["comments"]["inserted"] == 2

    rows = {c.id: c for c in db.query(VideoComment).all()}
    assert set(rows) == {"top", "top.r1"}
    # channel-owner resolution flows through to is_by_uploader.
    assert rows["top"].is_by_uploader is True
    assert rows["top.r1"].is_by_uploader is False
    assert rows["top.r1"].parent_comment_id == "top"
    # is_pinned is not something yt-dlp gives us; the engine hard-codes False.
    assert rows["top"].is_pinned is False

    done = db.get(SyncJob, job.id)
    assert done.status == "done"
    assert done.progress == 1.0
    video = (
        db.query(UserChannelVideo).filter_by(video_id=VIDEO_ID).first()
    )
    assert video.last_comments_sync_at is not None


def test_incomplete_fetch_never_soft_deletes(db):
    """Guard 2: complete=False is insert/update-only. A pre-existing comment
    absent from this fetch must survive untouched even though it is missing."""
    _seed(db)
    _existing(db, "old1", last_seen_days_ago=200)
    job = _job(db)
    payload = {
        "comments": {
            "complete": False,
            "reportedTotal": 999,
            "items": [_pi(id="new1", parentId=None)],
        }
    }

    out = _complete_comment_job(
        db, job=job, payload=payload, now=datetime.now(timezone.utc)
    )

    assert out["allowDeletions"] is False
    assert db.get(VideoComment, "old1").deleted_at is None
    # inserts still happen while deletions are withheld.
    assert db.get(VideoComment, "new1") is not None


def test_truncated_complete_fetch_suppresses_deletion(db):
    """Guard 3: even complete=True is refused deletions when the fetched set is
    implausibly short against reportedTotal - a bot-check that still exited 0."""
    _seed(db)
    _existing(db, "gone", last_seen_days_ago=200)
    job = _job(db)
    payload = {
        "comments": {
            "complete": True,
            "reportedTotal": 100,
            "items": [_pi(id="a"), _pi(id="b")],
        }
    }

    out = _complete_comment_job(
        db, job=job, payload=payload, now=datetime.now(timezone.utc)
    )

    assert out["allowDeletions"] is False
    assert db.get(VideoComment, "gone").deleted_at is None


def test_complete_debounced_fetch_soft_deletes_a_long_missing_comment(db):
    """The one path that DOES delete: complete, plausible, and the comment was
    already missing longer than one cadence (default quarterly = 90d)."""
    _seed(db)
    _existing(db, "gone", last_seen_days_ago=120)
    job = _job(db)
    payload = {
        "comments": {
            "complete": True,
            "reportedTotal": 1,
            "items": [_pi(id="stillhere", parentId=None)],
        }
    }

    out = _complete_comment_job(
        db, job=job, payload=payload, now=datetime.now(timezone.utc)
    )

    assert out["allowDeletions"] is True
    assert db.get(VideoComment, "gone").deleted_at is not None


def test_recent_miss_is_not_deleted_yet_even_when_complete(db):
    """Guard 4 debounce: a comment missing for the first time (last seen well
    within the cadence) is left alone; deletion needs a second consecutive
    complete-fetch miss."""
    _seed(db)
    _existing(db, "fresh_miss", last_seen_days_ago=1)
    job = _job(db)
    payload = {
        "comments": {
            "complete": True,
            "reportedTotal": 1,
            "items": [_pi(id="stillhere", parentId=None)],
        }
    }

    out = _complete_comment_job(
        db, job=job, payload=payload, now=datetime.now(timezone.utc)
    )

    assert out["allowDeletions"] is True
    assert db.get(VideoComment, "fresh_miss").deleted_at is None


def test_unusable_payload_fails_loudly(db):
    _seed(db)
    job = _job(db)
    with pytest.raises(HTTPException) as ei:
        _complete_comment_job(
            db, job=job, payload={}, now=datetime.now(timezone.utc)
        )
    assert ei.value.status_code == 400
    assert db.get(SyncJob, job.id).status == "failed"


def test_missing_video_row_fails_loudly(db):
    """A usable (even empty) payload but no video row we hold is a failure, not
    an empty success - never record a no-op as done."""
    _seed(db, with_video=False)
    job = _job(db)
    payload = {"comments": {"complete": True, "reportedTotal": 0, "items": []}}
    with pytest.raises(HTTPException) as ei:
        _complete_comment_job(
            db, job=job, payload=payload, now=datetime.now(timezone.utc)
        )
    assert ei.value.status_code == 400
    assert db.get(SyncJob, job.id).status == "failed"
