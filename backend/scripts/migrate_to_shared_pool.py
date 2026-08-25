"""One-shot migration: legacy per-user archive → shared-pool model.

Populates the new tables (Channel, Video, UserChannelSubscription,
ChannelOwnership) from the existing per-user tables (user_channels,
user_channel_videos, storage_objects). Idempotent — re-running on
already-migrated data is a no-op.

The old tables stay in place after this runs. The cutover (when route
code starts reading from the new tables instead of the old) is a
separate step. This script just gets the new tables populated and
matching the old data faithfully.

What gets created:

  Channel (one per unique youtube_channel_id):
    - youtube_id from user_channels.channel_id
    - title + handle parsed from user_channels.data_json

  Video (one per unique youtube_video_id):
    - youtube_id from user_channel_videos.video_id
    - title / description / published_at / duration_seconds / thumbnail_url
      from user_channel_videos.data_json
    - privacy_at_discovery + privacy_current from data_json.privacy
    - r2_key + bytes_stored matched from storage_objects (kind='video')
    - synced_at from data_json.archivedAt

  UserChannelSubscription (one per user_channels row):
    - user_id + channel_id from user_channels
    - subscribed_at from user_channels.added_at
    - unsubscribed_at from user_channels.removed_at (preserves the
      soft-delete state so the 30-day grace window honors the
      original removal time)

  ChannelOwnership (one per user_channels row that had a
  google_user_id - the user demonstrably had access to that channel,
  which is the substantive thing ownership represents in the new
  model. Pre-OAuth Basic-tier users who synced via worker cookies
  still get an ownership record here so they don't lose access to
  their own private videos post-cutover.):
    - user_id + channel_id from user_channels
    - google_user_id from user_channels.google_user_id
    - authenticated_at from user_channels.added_at

Usage:
    /opt/aether/venv/bin/python -m scripts.migrate_to_shared_pool --dry-run
    /opt/aether/venv/bin/python -m scripts.migrate_to_shared_pool
"""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

from app.db import SessionLocal
from app.models import visibility_for_privacy
from app.models import (
    Channel,
    ChannelOwnership,
    UserChannelSubscription,
    Video,
)


log = logging.getLogger("aether.migrate_shared_pool")


# Map legacy privacy strings to the new tier enum. The legacy
# data_json sometimes uses YouTube's API strings ("public" /
# "unlisted" / "private") and sometimes our own ("members_only"
# wouldn't appear in old data since we didn't track them). Unknown
# values default to "public" - the least restrictive, which means
# the worst case is we surface a video to a user who might not
# strictly be entitled to it. Since this is migrating a single
# user's own archive, that risk is zero.
_PRIVACY_MAP = {
    "public": "public",
    "unlisted": "unlisted",
    "private": "private",
    "members_only": "members_only",
    "membersonly": "members_only",
    # The spelling _privacy_from_availability actually writes.
    "members": "members",
    "age_restricted": "age_restricted",
}


# storage_objects.r2_key for videos is "videos/<youtube_id>/video.mp4".
# Pre-existing artifact paths like "users/<uid>/videos/<id>/video.mp4"
# also exist; we ignore those and prefer the new-shape ones.
_R2_KEY_VIDEO_ID = re.compile(r"^videos/([A-Za-z0-9_-]+)/video\.mp4$")

# Thumbnails sit alongside video files at "thumbnails/<youtube_id>.jpg".
# Billing includes them in Video.bytes_stored so the v2 totals match
# v1 (which sums every kind in storage_objects).
_R2_KEY_THUMBNAIL_ID = re.compile(r"^thumbnails/([A-Za-z0-9_-]+)\.jpg$")


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    """Best-effort ISO-8601 parser. Returns None on any failure so a
    single malformed timestamp doesn't kill the migration."""
    if not s:
        return None
    try:
        # ISO strings ending in 'Z' aren't accepted by fromisoformat
        # on Python < 3.11.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _ensure_aware(dt) -> Optional[datetime]:
    """Accept either a datetime or an ISO-string (SQLite raw returns
    strings for DATETIME columns) and produce a tz-aware datetime."""
    if dt is None:
        return None
    if isinstance(dt, str):
        return _parse_iso(dt)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _map_privacy(raw: Optional[str]) -> str:
    if not raw:
        return "public"
    return _PRIVACY_MAP.get(raw.lower(), "public")


def _video_r2_match(r2_key: str) -> Optional[str]:
    m = _R2_KEY_VIDEO_ID.match(r2_key)
    return m.group(1) if m else None


def main(argv: list[str]) -> int:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )
    dry_run = "--dry-run" in argv

    db = SessionLocal()
    try:
        # ---------- index existing storage objects by video_id ----------
        # For each youtube_id we want:
        #   - canonical r2_key + bytes (kind='video', new-shape path)
        #   - thumbnail bytes (kind='thumbnail') to roll into total
        #   - EARLIEST uploaded_at across video-kind rows for the same
        #     youtube_id, since that's when R2 storage starts
        #     accruing (a later re-upload artifact is just trash).
        storage_index: dict[str, dict] = {}
        thumb_bytes_by_yt: dict[str, int] = {}
        rows = db.execute(
            text(
                "SELECT r2_key, bytes, uploaded_at, kind FROM storage_objects "
                "WHERE deleted_at IS NULL"
            )
        ).all()
        for r2_key, byts, uploaded_at, kind in rows:
            up_dt = _parse_iso(uploaded_at) if isinstance(uploaded_at, str) else _ensure_aware(uploaded_at)
            if kind == "video":
                vid = _video_r2_match(r2_key)
                if vid is None:
                    continue  # legacy/non-canonical path; skip
                cur = storage_index.get(vid)
                if cur is None:
                    storage_index[vid] = {
                        "r2_key": r2_key,
                        "bytes": byts,
                        "uploaded_at": up_dt,
                    }
                else:
                    # Keep the earliest uploaded_at; canonical r2_key
                    # + bytes stay as-is (first canonical row wins).
                    if up_dt is not None and (
                        cur["uploaded_at"] is None
                        or up_dt < cur["uploaded_at"]
                    ):
                        cur["uploaded_at"] = up_dt
            elif kind == "thumbnail":
                m = _R2_KEY_THUMBNAIL_ID.match(r2_key)
                if m is None:
                    continue
                thumb_bytes_by_yt[m.group(1)] = byts
        log.info(
            "indexed %d video storage objects, %d thumbnails",
            len(storage_index),
            len(thumb_bytes_by_yt),
        )

        # ---------- migrate channels + subscriptions + ownerships ------
        channels_by_youtube_id: dict[str, Channel] = {}
        created_channels = 0
        created_subs = 0
        created_owns = 0

        legacy_channels = db.execute(
            text(
                "SELECT user_id, channel_id, google_user_id, data_json, "
                "added_at, removed_at FROM user_channels"
            )
        ).all()
        log.info("processing %d legacy user_channels rows", len(legacy_channels))

        for (
            user_id,
            channel_youtube_id,
            google_user_id,
            data_json_raw,
            added_at,
            removed_at,
        ) in legacy_channels:
            try:
                cd = json.loads(data_json_raw) if data_json_raw else {}
            except Exception:
                cd = {}

            existing_ch = (
                db.query(Channel)
                .filter(Channel.youtube_id == channel_youtube_id)
                .one_or_none()
            )
            if existing_ch is None:
                ch = Channel(
                    youtube_id=channel_youtube_id,
                    handle=cd.get("handle") or cd.get("customUrl"),
                    title=cd.get("title")
                    or cd.get("name")
                    or f"Channel {channel_youtube_id}",
                    thumbnail_url=cd.get("thumbnailUrl") or cd.get("avatarUrl"),
                )
                db.add(ch)
                db.flush()
                created_channels += 1
                log.info(
                    "  created Channel %s -> %s (%s)",
                    channel_youtube_id,
                    ch.id,
                    ch.title,
                )
            else:
                ch = existing_ch
                log.info(
                    "  Channel %s already exists -> %s",
                    channel_youtube_id,
                    ch.id,
                )
            channels_by_youtube_id[channel_youtube_id] = ch

            # Subscription
            existing_sub = (
                db.query(UserChannelSubscription)
                .filter(
                    UserChannelSubscription.user_id == user_id,
                    UserChannelSubscription.channel_id == ch.id,
                )
                .one_or_none()
            )
            if existing_sub is None:
                sub = UserChannelSubscription(
                    user_id=user_id,
                    channel_id=ch.id,
                    subscribed_at=_ensure_aware(added_at)
                    or datetime.now(timezone.utc),
                    unsubscribed_at=_ensure_aware(removed_at),
                )
                db.add(sub)
                created_subs += 1
                log.info(
                    "    created UserChannelSubscription user=%s removed_at=%s",
                    user_id,
                    removed_at,
                )

            # Ownership - only when we have a google_user_id, which
            # is the proof of access we're preserving from the legacy
            # model.
            if google_user_id:
                existing_own = (
                    db.query(ChannelOwnership)
                    .filter(
                        ChannelOwnership.user_id == user_id,
                        ChannelOwnership.channel_id == ch.id,
                    )
                    .one_or_none()
                )
                if existing_own is None:
                    own = ChannelOwnership(
                        user_id=user_id,
                        channel_id=ch.id,
                        google_user_id=google_user_id,
                        authenticated_at=_ensure_aware(added_at)
                        or datetime.now(timezone.utc),
                    )
                    db.add(own)
                    created_owns += 1
                    log.info(
                        "    created ChannelOwnership google_user_id=%s",
                        google_user_id,
                    )

        # ---------- migrate videos ------------------------------------
        created_videos = 0
        skipped_videos = 0
        legacy_videos = db.execute(
            text(
                "SELECT user_id, channel_id, video_id, data_json, "
                "discovered_at, updated_at FROM user_channel_videos"
            )
        ).all()
        log.info(
            "processing %d legacy user_channel_videos rows",
            len(legacy_videos),
        )

        for (
            user_id,
            channel_youtube_id,
            video_youtube_id,
            data_json_raw,
            discovered_at,
            updated_at,
        ) in legacy_videos:
            ch = channels_by_youtube_id.get(channel_youtube_id)
            if ch is None:
                log.warning(
                    "  video %s references unknown channel %s; skipping",
                    video_youtube_id,
                    channel_youtube_id,
                )
                skipped_videos += 1
                continue

            try:
                vd = json.loads(data_json_raw) if data_json_raw else {}
            except Exception:
                vd = {}

            existing_v = (
                db.query(Video)
                .filter(Video.youtube_id == video_youtube_id)
                .one_or_none()
            )
            if existing_v is not None:
                log.info(
                    "  Video %s already migrated -> %s",
                    video_youtube_id,
                    existing_v.id,
                )
                continue

            storage = storage_index.get(video_youtube_id)
            r2_key = storage["r2_key"] if storage else None
            bytes_stored = storage["bytes"] if storage else None
            # Roll thumbnail bytes into the same Video row's
            # bytes_stored so v1 (which sums all storage kinds) and
            # v2 stay aligned for billing.
            if bytes_stored is not None:
                bytes_stored += thumb_bytes_by_yt.get(video_youtube_id, 0)

            privacy = _map_privacy(vd.get("privacy"))
            published_at = (
                _parse_iso(vd.get("uploadDate"))
                or _parse_iso(vd.get("publishedAt"))
                or _ensure_aware(discovered_at)
                or datetime.now(timezone.utc)
            )
            # synced_at = when bytes first hit R2, not the data_json
            # archivedAt (which may match a re-upload artifact).
            synced_at = (
                storage["uploaded_at"]
                if storage and storage.get("uploaded_at")
                else (
                    _parse_iso(vd.get("archivedAt"))
                    or _ensure_aware(updated_at)
                )
            )

            video = Video(
                channel_id=ch.id,
                youtube_id=video_youtube_id,
                title=vd.get("title") or video_youtube_id,
                description=vd.get("description"),
                thumbnail_url=vd.get("thumbnailUrl"),
                published_at=published_at,
                duration_seconds=vd.get("durationSec"),
                privacy_at_discovery=privacy,
                privacy_current=privacy,
                # Stamp visibility like archive.record_synced_video does.
                # Omitting it let the column default to "open", which would
                # have migrated every private / unlisted / members-only
                # video in as publicly visible to the whole shared pool.
                # (Checked prod after wiring the access filter: zero rows
                # were actually corrupted, so this is a live hazard for any
                # future run rather than damage to repair.)
                visibility=visibility_for_privacy(privacy),
                r2_key=r2_key,
                bytes_stored=bytes_stored,
                synced_at=synced_at if r2_key else None,
            )
            db.add(video)
            db.flush()
            created_videos += 1
            log.info(
                "  created Video %s -> %s privacy=%s bytes=%s",
                video_youtube_id,
                video.id,
                privacy,
                bytes_stored,
            )

        log.info(
            "summary: channels=%d subscriptions=%d ownerships=%d "
            "videos=%d skipped_videos=%d",
            created_channels,
            created_subs,
            created_owns,
            created_videos,
            skipped_videos,
        )

        if dry_run:
            log.info("dry-run: rolling back changes")
            db.rollback()
        else:
            db.commit()
            log.info("committed.")
        return 0
    except Exception:
        log.exception("migration failed")
        db.rollback()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
