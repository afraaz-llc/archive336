"""Canonical R2 key paths.

Every R2 PUT in the backend should construct keys through these
helpers so the per-user-prefix layout from
``docs/STORAGE_BILLING_DESIGN.md`` stays consistent and
enumeration (e.g. reconciliation walks) can rely on the format.

New layout (Phase C onward):

    users/{user_id}/videos/{video_id}/video.mp4
    users/{user_id}/videos/{video_id}/thumb.jpg
    users/{user_id}/videos/{video_id}/captions/{lang}.vtt
    users/{user_id}/videos/{video_id}/thumb_history/{ts}.jpg
    users/{user_id}/channels/{channel_id}/avatar.jpg

Old layout (legacy keys before Phase C, still valid for already-
uploaded files since R2 doesn't care about path conventions):

    videos/{video_id}/video.mp4
    videos/{video_id}/captions/{lang}.vtt
    thumbnails/{video_id}.jpg
    thumbnails/{video_id}/history/{ts}.jpg
    avatars/{channel_id}.jpg

Reads should use whatever key is stored in the DB (``localPath``,
``thumbnail_r2_key``, ``avatar_r2_key``) rather than re-constructing
from the layout — the stored value reflects the layout at upload time
and so handles both layouts transparently. For captions (which aren't
stored as keys), derive the captions base from the video's stored
``localPath`` instead of hardcoding the layout.
"""
from __future__ import annotations


def user_prefix(user_id: str) -> str:
    """Top-level prefix for a single user's objects. Always ends with /."""
    return f"users/{user_id}/"


def video_key(user_id: str, video_id: str) -> str:
    return f"users/{user_id}/videos/{video_id}/video.mp4"


def thumb_key(user_id: str, video_id: str) -> str:
    return f"users/{user_id}/videos/{video_id}/thumb.jpg"


def caption_key(user_id: str, video_id: str, language: str) -> str:
    return f"users/{user_id}/videos/{video_id}/captions/{language}.vtt"


def caption_history_key(
    user_id: str, video_id: str, language: str, timestamp_iso: str
) -> str:
    """Versioned caption snapshot: the prior ``.vtt`` for a language,
    preserved when the transcript changes (analogous to thumb_history_key).
    ``timestamp_iso`` should be filename-safe (no colons/spaces)."""
    return (
        f"users/{user_id}/videos/{video_id}/caption_history/"
        f"{language}/{timestamp_iso}.vtt"
    )


def captions_base_for_video(video_r2_key: str) -> str:
    """Given a video's stored R2 key (which may be in the old layout
    ``videos/{vid}/video.mp4`` or the new layout
    ``users/{uid}/videos/{vid}/video.mp4``), return the directory
    captions live under.

    Returns empty string if the key doesn't end in /video.mp4 (legacy
    desktop-filesystem paths, etc).
    """
    if video_r2_key.endswith("/video.mp4"):
        return video_r2_key[: -len("/video.mp4")]
    return ""


def avatar_key(user_id: str, channel_id: str) -> str:
    return f"users/{user_id}/channels/{channel_id}/avatar.jpg"


def avatar_history_key(user_id: str, channel_id: str, timestamp_iso: str) -> str:
    """Versioned channel-avatar snapshot: the prior profile picture,
    preserved when the channel changes it. ``timestamp_iso`` should be
    filename-safe (no colons/spaces)."""
    return (
        f"users/{user_id}/channels/{channel_id}/avatar_history/"
        f"{timestamp_iso}.jpg"
    )


def thumb_history_key(user_id: str, video_id: str, timestamp_iso: str) -> str:
    """Versioned thumbnail snapshot from a metadata rescan rotation.

    ``timestamp_iso`` should be a filename-safe timestamp string (no
    colons or spaces). Callers typically pass an ISO timestamp with
    ``:`` replaced by ``-``.
    """
    return (
        f"users/{user_id}/videos/{video_id}/thumb_history/{timestamp_iso}.jpg"
    )
