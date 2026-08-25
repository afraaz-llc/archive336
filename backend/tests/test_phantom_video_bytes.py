"""Video.bytes_stored must equal what we actually store, never phantom bytes.

The `videos` table is channel-keyed. When a user is deleted their storage is
purged and storage_objects.deleted_at flips, but a Video row (no user FK)
survives with the dead user's r2_key and bytes_stored. Billing sums
bytes_stored with no storage join, so a survivor overbills for storage we no
longer hold. These pin the two guards that keep bytes_stored truthful: the
deletion-time null-out, and the reconcile self-heal - and, above all, that
neither ever clears a genuinely-stored video.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app import archive
from app.models import StorageObject, User, Video


@pytest.fixture(autouse=True)
def _owner(db):
    """StorageObject.user_id is a real FK. Every backed video needs it."""
    db.add(
        User(
            id="u-owner",
            username="owner",
            email="owner@example.com",
            password_hash="$2b$12$placeholder",
        )
    )
    db.flush()


def _stored_video(db, youtube_id, r2_key, bytes_stored, *, backed=True):
    ch = archive.ensure_channel(db, "UC" + youtube_id)
    v = Video(
        channel_id=ch.id,
        youtube_id=youtube_id,
        title=youtube_id,
        published_at=datetime.now(timezone.utc),
        privacy_at_discovery="public",
        privacy_current="public",
        r2_key=r2_key,
        bytes_stored=bytes_stored,
        synced_at=datetime.now(timezone.utc),
        visibility="open",
    )
    db.add(v)
    if backed:
        db.add(
            StorageObject(
                user_id="u-owner",
                r2_key=r2_key,
                bytes=bytes_stored,
                metadata_bytes=256,
                kind="video",
                uploaded_at=datetime.now(timezone.utc),
            )
        )
    db.flush()
    return v


# The reconcile guard is a script; run its exact NOT-EXISTS detector as SQL so
# the test pins the query the backfill and self-heal both rely on.
_PHANTOM_SELECT = text(
    """
    SELECT v.youtube_id
    FROM videos v
    WHERE v.bytes_stored IS NOT NULL
      AND v.bytes_stored > 0
      AND NOT EXISTS (
            SELECT 1 FROM storage_objects s
            WHERE s.r2_key = v.r2_key AND s.deleted_at IS NULL
          )
    """
)


def _phantoms(db):
    return {row.youtube_id for row in db.execute(_PHANTOM_SELECT).fetchall()}


def test_backed_video_is_never_flagged(db):
    _stored_video(db, "real1", "videos/real1/video.mp4", 1000, backed=True)
    assert _phantoms(db) == set()


def test_video_whose_storage_was_deleted_is_flagged(db):
    v = _stored_video(db, "gone1", "users/dead/videos/gone1/video.mp4", 5000, backed=True)
    # Simulate the user deletion: storage object soft-deleted, Video survives.
    db.query(StorageObject).filter(StorageObject.r2_key == v.r2_key).update(
        {StorageObject.deleted_at: datetime.now(timezone.utc)},
        synchronize_session=False,
    )
    db.flush()
    assert _phantoms(db) == {"gone1"}


def test_video_with_bytes_but_no_key_is_flagged(db):
    v = _stored_video(db, "nokey", "videos/nokey/video.mp4", 3000, backed=False)
    v.r2_key = None
    db.flush()
    assert _phantoms(db) == {"nokey"}


def test_mixed_channel_flags_only_the_unbacked(db):
    _stored_video(db, "keep1", "videos/keep1/video.mp4", 1000, backed=True)
    _stored_video(db, "keep2", "videos/keep2/video.mp4", 2000, backed=True)
    dead = _stored_video(db, "dead1", "users/x/videos/dead1/video.mp4", 9000, backed=True)
    db.query(StorageObject).filter(StorageObject.r2_key == dead.r2_key).update(
        {StorageObject.deleted_at: datetime.now(timezone.utc)},
        synchronize_session=False,
    )
    db.flush()
    assert _phantoms(db) == {"dead1"}


def test_tracked_not_downloaded_video_is_not_flagged(db):
    """bytes_stored NULL is the legitimate tracked-but-not-downloaded state
    and must never be treated as phantom."""
    ch = archive.ensure_channel(db, "UCtracked")
    db.add(
        Video(
            channel_id=ch.id,
            youtube_id="tracked1",
            title="tracked1",
            published_at=datetime.now(timezone.utc),
            privacy_at_discovery="public",
            privacy_current="public",
            r2_key=None,
            bytes_stored=None,
            synced_at=None,
            visibility="open",
        )
    )
    db.flush()
    assert _phantoms(db) == set()
