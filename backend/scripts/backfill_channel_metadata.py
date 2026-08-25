"""One-off: populate Channel.metadata_json + UserChannelSubscription
.settings_json + .last_synced_at from the matching legacy
UserChannel.data_json.

Mirrors the backfill_video_metadata_json script's shape. Required
before the read-route cutover can render the same fields the YouTube
page reads from UserChannel.data_json today (subscriberCount, country,
joinedAt, links, addedAt, settings, lastSyncedAt, etc.).

Channel.metadata_json holds only the YouTube-side fields (same for
every subscriber). Per-user fields (settings, lastSyncedAt) split off
to UserChannelSubscription so two subscribers can have independent
state. addedAt isn't backfilled here - UserChannelSubscription.
subscribed_at already stores that.

Idempotent: skips rows whose target columns are already populated.

Usage:
    /opt/aether/venv/bin/python -m scripts.backfill_channel_metadata --dry-run
    /opt/aether/venv/bin/python -m scripts.backfill_channel_metadata
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

from app.db import SessionLocal
from app.models import Channel, UserChannelSubscription


log = logging.getLogger("aether.backfill_channel_metadata")


# Fields we strip out of the legacy data_json before storing as
# Channel.metadata_json — these are either per-user (settings,
# addedAt, lastSyncedAt) or already structured on the Channel /
# Subscription columns (id, name, handle, avatarUrl).
_PER_USER_FIELDS = {"settings", "addedAt", "lastSyncedAt"}
_STRUCTURED_FIELDS = {"id", "name", "handle", "avatarUrl"}


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def main(argv: list[str]) -> int:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )
    dry_run = "--dry-run" in argv

    db = SessionLocal()
    try:
        # ALTER columns if missing (SQLAlchemy create_all doesn't
        # touch existing tables).
        cols_ch = {
            row[1]
            for row in db.execute(text("PRAGMA table_info(channels)")).all()
        }
        if "metadata_json" not in cols_ch:
            log.info("adding column channels.metadata_json")
            if not dry_run:
                db.execute(text("ALTER TABLE channels ADD COLUMN metadata_json TEXT"))
                db.commit()
        if "avatar_r2_key" not in cols_ch:
            log.info("adding column channels.avatar_r2_key")
            if not dry_run:
                db.execute(
                    text("ALTER TABLE channels ADD COLUMN avatar_r2_key VARCHAR")
                )
                db.commit()

        cols_sub = {
            row[1]
            for row in db.execute(
                text("PRAGMA table_info(user_channel_subscriptions)")
            ).all()
        }
        if "settings_json" not in cols_sub:
            log.info(
                "adding column user_channel_subscriptions.settings_json"
            )
            if not dry_run:
                db.execute(
                    text(
                        "ALTER TABLE user_channel_subscriptions ADD COLUMN settings_json TEXT"
                    )
                )
                db.commit()
        if "last_synced_at" not in cols_sub:
            log.info(
                "adding column user_channel_subscriptions.last_synced_at"
            )
            if not dry_run:
                db.execute(
                    text(
                        "ALTER TABLE user_channel_subscriptions ADD COLUMN last_synced_at DATETIME"
                    )
                )
                db.commit()

        # Index UserChannel rows by (channel youtube_id) - we'll
        # need to look up legacy data per-user-channel and per-channel.
        legacy_rows = db.execute(
            text(
                "SELECT user_id, channel_id, data_json, avatar_r2_key FROM user_channels"
            )
        ).all()
        legacy_by_channel_yt: dict[str, dict] = {}
        legacy_avatar_by_channel_yt: dict[str, str] = {}
        legacy_by_user_channel: dict[tuple[str, str], dict] = {}
        for user_id, channel_yt_id, data_json_raw, avatar_key in legacy_rows:
            if data_json_raw:
                try:
                    d = json.loads(data_json_raw)
                    legacy_by_channel_yt.setdefault(channel_yt_id, d)
                    legacy_by_user_channel[(user_id, channel_yt_id)] = d
                except json.JSONDecodeError:
                    pass
            if avatar_key:
                legacy_avatar_by_channel_yt.setdefault(
                    channel_yt_id, avatar_key
                )
        log.info(
            "indexed %d legacy user_channels (%d unique channels, %d with avatars)",
            len(legacy_by_user_channel),
            len(legacy_by_channel_yt),
            len(legacy_avatar_by_channel_yt),
        )

        # Walk Channel rows, populate metadata_json + avatar_r2_key.
        ch_updates = 0
        for channel in db.query(Channel).all():
            changed = False
            if not channel.metadata_json:
                legacy = legacy_by_channel_yt.get(channel.youtube_id)
                if legacy:
                    cleaned = {
                        k: v
                        for k, v in legacy.items()
                        if k not in _PER_USER_FIELDS
                        and k not in _STRUCTURED_FIELDS
                    }
                    channel.metadata_json = json.dumps(cleaned)
                    changed = True
            if not channel.avatar_r2_key:
                ak = legacy_avatar_by_channel_yt.get(channel.youtube_id)
                if ak:
                    channel.avatar_r2_key = ak
                    changed = True
            if changed:
                ch_updates += 1

        # Walk Subscription rows, populate settings_json + last_synced_at.
        sub_updates = 0
        for sub in db.query(UserChannelSubscription).all():
            # Need the channel's youtube_id to match against legacy.
            channel = db.get(Channel, sub.channel_id)
            if channel is None:
                continue
            legacy = legacy_by_user_channel.get(
                (sub.user_id, channel.youtube_id)
            )
            if not legacy:
                continue
            changed = False
            if sub.settings_json is None and legacy.get("settings"):
                sub.settings_json = json.dumps(legacy["settings"])
                changed = True
            if sub.last_synced_at is None:
                lsa = _parse_iso(legacy.get("lastSyncedAt"))
                if lsa is not None:
                    sub.last_synced_at = lsa
                    changed = True
            if changed:
                sub_updates += 1

        log.info(
            "summary: channels=%d subscriptions=%d",
            ch_updates,
            sub_updates,
        )
        if dry_run:
            log.info("dry-run: rolling back")
            db.rollback()
        else:
            db.commit()
            log.info("committed.")
        return 0
    except Exception:
        log.exception("backfill failed")
        db.rollback()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
