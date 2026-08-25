"""Deleting an account deletes the account's video files.

Found by an audit of the storage migration. _r2_keys_for_user tested
`local_path.startswith("videos/")`, which stopped matching anything the
day uploads moved to `users/{uid}/videos/...`. So account deletion
removed thumbnails and avatars and left every mp4 and every .vtt caption
in the bucket - the user's data outliving the deletion they asked for,
and us paying to store it forever.

It is the same bug that made the channel purge leave 852 MB behind, in a
second hand-rolled copy of the same enumeration that nobody migrated.
Both now delegate to storage_ledger.keys_from_video_data.
"""
from __future__ import annotations

import json

from app.models import User, UserChannel, UserChannelVideo
from app.routes.auth import _r2_keys_for_user


def _user(db, uid="u-del"):
    u = User(id=uid, username=uid, email=f"{uid}@x.com", password_hash="p")
    db.add(u)
    db.flush()
    return u


def _video(db, user, video_id, local_path, *, langs=None, thumb=None):
    row = UserChannelVideo(
        user_id=user.id,
        channel_id="UCx",
        video_id=video_id,
        thumbnail_r2_key=thumb,
        data_json=json.dumps({
            "localPath": local_path,
            "captionLanguages": langs or [],
        }),
    )
    db.add(row)
    db.flush()
    return row


def test_current_layout_video_is_deleted(db):
    """The regression. This key was invisible to the old prefix test."""
    u = _user(db)
    _video(db, u, "v1", "users/u-del/videos/v1/video.mp4")

    keys = _r2_keys_for_user(db, u.id)

    assert "users/u-del/videos/v1/video.mp4" in keys, (
        "an mp4 in the current key layout must be deleted with the account"
    )


def test_captions_are_deleted_too(db):
    u = _user(db)
    _video(db, u, "v1", "users/u-del/videos/v1/video.mp4", langs=["en", "es"])

    keys = _r2_keys_for_user(db, u.id)

    assert "users/u-del/videos/v1/captions/en.vtt" in keys
    assert "users/u-del/videos/v1/captions/es.vtt" in keys


def test_legacy_layout_still_deleted(db):
    """Old rows must not be stranded by the fix."""
    u = _user(db)
    _video(db, u, "v1", "videos/v1/video.mp4")

    assert "videos/v1/video.mp4" in _r2_keys_for_user(db, u.id)


def test_thumbnails_and_avatars_still_included(db):
    u = _user(db)
    _video(db, u, "v1", "users/u-del/videos/v1/video.mp4", thumb="users/u-del/thumbs/v1.jpg")
    db.add(UserChannel(
        user_id=u.id, channel_id="UCx", google_user_id=None,
        data_json="{}", avatar_r2_key="users/u-del/channels/UCx/avatar.jpg",
    ))
    db.flush()

    keys = _r2_keys_for_user(db, u.id)

    assert "users/u-del/thumbs/v1.jpg" in keys
    assert "users/u-del/channels/UCx/avatar.jpg" in keys


def test_desktop_filesystem_path_is_never_deleted(db):
    """The original guard existed for a real reason: localPath could hold
    a laptop path from the pre-MVP days. Never hand that to a delete."""
    u = _user(db)
    _video(db, u, "v1", "/Users/bob/Movies/holiday.mp4")

    assert _r2_keys_for_user(db, u.id) == []
