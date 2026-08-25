"""Archive notifications — the send paths behind the notification toggles.

Every toggle in the settings panel's Archive-integrity / Activity sections
routes through here, so a toggle either genuinely controls an email or
doesn't exist. Before this module the toggles were inert: three had no send
path at all, and OAuth-disconnected fired regardless of its switch.

Scope of each flag:
  - per-channel (stored in that channel's settings):
      notifyVideoDeleted, notifyChannelTerminated, notifyNewUpload,
      notifyOauthDisconnected
  - per-user (stored in the user's global YouTube settings):
      notifyMonthlyDigest — it's one email covering the whole account, so a
      per-channel switch makes no sense for it.

Defaults mirror the frontend: integrity alerts default ON (they're the
reason people archive), activity ones default OFF (don't spam).

Every send is best-effort: failures are logged and swallowed so a Resend
hiccup never breaks a sync, a rescan, or a webhook.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app import email as email_lib
from app.models import EmailSendLog, User, UserChannel, UserYouTubeSettings

log = logging.getLogger("aether.notify")

SITE = "https://archive336.com"

# Flag -> default when the setting is absent. Matches defaultChannelSettings.
_DEFAULTS: Dict[str, bool] = {
    "notifyVideoDeleted": True,
    "notifyChannelTerminated": True,
    "notifyOauthDisconnected": True,
    "notifyNewUpload": False,
    "notifyMonthlyDigest": False,
}


def _settings_for_channel(
    db: Session, user_id: str, channel_youtube_id: str
) -> Dict[str, Any]:
    row = db.get(UserChannel, (user_id, channel_youtube_id))
    if row is None:
        return {}
    try:
        return (json.loads(row.data_json) or {}).get("settings") or {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _settings_for_user(db: Session, user_id: str) -> Dict[str, Any]:
    row = db.get(UserYouTubeSettings, user_id)
    if row is None:
        return {}
    try:
        return json.loads(row.settings_json) or {}
    except (json.JSONDecodeError, TypeError):
        return {}


def channel_flag(
    db: Session, user_id: str, channel_youtube_id: str, flag: str
) -> bool:
    """Whether this per-channel notification is on for this user+channel."""
    default = _DEFAULTS.get(flag, False)
    settings = _settings_for_channel(db, user_id, channel_youtube_id)
    if flag in settings:
        return bool(settings.get(flag))
    # Fall back to the user's global default before the hardcoded one, so a
    # user who set a preference globally gets it on channels saved earlier.
    user_settings = _settings_for_user(db, user_id)
    if flag in user_settings:
        return bool(user_settings.get(flag))
    return default


def user_flag(db: Session, user_id: str, flag: str) -> bool:
    """Whether this account-level notification is on."""
    settings = _settings_for_user(db, user_id)
    if flag in settings:
        return bool(settings.get(flag))
    return _DEFAULTS.get(flag, False)


def _recipient(db: Session, user_id: str) -> Optional[str]:
    user = db.get(User, user_id)
    if user is None or not user.email:
        return None
    return user.email


def _record(db: Session, kind: str, to_email: str) -> None:
    try:
        db.add(EmailSendLog(type=kind, to_email=to_email))
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


def notify_video_deleted(
    db: Session,
    *,
    user_id: str,
    channel_youtube_id: str,
    channel_name: str,
    count: int,
    video_id: Optional[str] = None,
    video_title: Optional[str] = None,
) -> bool:
    """Videos vanished from YouTube but we hold the archived copies.

    ``video_id``/``video_title`` are set only when exactly ONE video was
    confirmed removed, so the mail can name it and deep-link to it. With
    several in one sweep there is no single video to point at and the mail
    stays channel-level."""
    if count <= 0:
        return False
    if not channel_flag(db, user_id, channel_youtube_id, "notifyVideoDeleted"):
        return False
    to = _recipient(db, user_id)
    if not to:
        return False
    # /youtube/channel/<id> is the real route (App.tsx); the old
    # /youtube/<id> matched nothing, so every one of these buttons dropped
    # the user on a dead URL instead of the channel they were told about.
    url = f"{SITE}/youtube/channel/{channel_youtube_id}"
    try:
        # The video detail panel is URL-backed (?video=<id> on the channel
        # page), so a single removal can link straight at the video itself.
        if video_id:
            url = f"{url}?video={video_id}"
        email_lib.send_video_deleted(
            to, channel_name, count, url, video_title=video_title
        )
    except Exception:  # noqa: BLE001
        log.exception("failed to send video-deleted email to %s", to)
        return False
    _record(db, "video_deleted", to)
    return True


def notify_channel_terminated(
    db: Session,
    *,
    user_id: str,
    channel_youtube_id: str,
    channel_name: str,
) -> bool:
    if not channel_flag(
        db, user_id, channel_youtube_id, "notifyChannelTerminated"
    ):
        return False
    to = _recipient(db, user_id)
    if not to:
        return False
    # /youtube/channel/<id> is the real route (App.tsx); the old
    # /youtube/<id> matched nothing, so every one of these buttons dropped
    # the user on a dead URL instead of the channel they were told about.
    url = f"{SITE}/youtube/channel/{channel_youtube_id}"
    try:
        email_lib.send_channel_terminated(to, channel_name, url)
    except Exception:  # noqa: BLE001
        log.exception("failed to send channel-terminated email to %s", to)
        return False
    _record(db, "channel_terminated", to)
    return True


def notify_new_upload(
    db: Session,
    *,
    user_id: str,
    channel_youtube_id: str,
    channel_name: str,
    video_title: str,
    video_id: Optional[str] = None,
) -> bool:
    if not channel_flag(db, user_id, channel_youtube_id, "notifyNewUpload"):
        return False
    to = _recipient(db, user_id)
    if not to:
        return False
    # /youtube/channel/<id> is the real route (App.tsx); the old
    # /youtube/<id> matched nothing, so every one of these buttons dropped
    # the user on a dead URL instead of the channel they were told about.
    url = f"{SITE}/youtube/channel/{channel_youtube_id}"
    try:
        # Open the video itself, not just the channel it landed on.
        if video_id:
            url = f"{url}?video={video_id}"
        email_lib.send_new_upload(to, channel_name, video_title, url)
    except Exception:  # noqa: BLE001
        log.exception("failed to send new-upload email to %s", to)
        return False
    _record(db, "new_upload", to)
    return True


def notify_monthly_digest(
    db: Session,
    *,
    user_id: str,
    archived: int,
    deletions_caught: int,
    storage_gb: float,
) -> bool:
    if not user_flag(db, user_id, "notifyMonthlyDigest"):
        return False
    to = _recipient(db, user_id)
    if not to:
        return False
    try:
        email_lib.send_monthly_digest(
            to,
            archived=archived,
            deletions_caught=deletions_caught,
            storage_gb=storage_gb,
            url=f"{SITE}/youtube",
        )
    except Exception:  # noqa: BLE001
        log.exception("failed to send monthly digest to %s", to)
        return False
    _record(db, "monthly_digest", to)
    return True
