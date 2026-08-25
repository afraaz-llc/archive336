"""Backfill StorageObject rows from existing data.

Phase A of docs/STORAGE_BILLING_DESIGN.md. Walks the existing
UserChannel + UserChannelVideo + VideoFieldSnapshot tables and creates
one StorageObject row per actual R2 object we know about.

Conservative timestamps: uploaded_at uses archivedAt if present, else
discovered_at / added_at / captured_at as appropriate. This slightly
over-bills existing data (we pretend it was uploaded earlier than it
might have been); we never under-bill.

For objects we have a size recorded in the DB (videos via
data_json.fileSizeBytes, thumbnails via thumbnail_size_bytes) we use
that. For objects without a stored size (channel avatars, caption
files, snapshot thumbnails) we HEAD the R2 object to get
ContentLength. If R2 doesn't have the object, we skip it (orphan in
our records vs reality — reconciliation will sort it out later).

Idempotent: if a StorageObject for a given r2_key already exists, skip
it. Safe to re-run.

Usage:
    /opt/aether/venv/bin/python -m scripts.backfill_storage_objects [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Optional

from app import r2
from app.db import SessionLocal
from app.models import (
    StorageObject,
    UserChannel,
    UserChannelVideo,
    VideoFieldSnapshot,
)


log = logging.getLogger("aether.backfill_storage")


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    """Parse an ISO 8601 string from data_json. Returns None if missing/malformed."""
    if not s or not isinstance(s, str):
        return None
    try:
        # Python's fromisoformat handles +HH:MM offsets and Z (3.11+).
        # Strip a trailing Z for older Pythons just in case.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _head_r2_size(key: str) -> Optional[int]:
    """HEAD an R2 object to get its ContentLength. Returns None if the object
    doesn't exist or R2 isn't configured."""
    c = r2.client()
    bucket = r2.bucket()
    if c is None or bucket is None:
        return None
    try:
        resp = c.head_object(Bucket=bucket, Key=key)
        return int(resp.get("ContentLength") or 0)
    except Exception as e:
        log.warning("HEAD %s failed: %s", key, e)
        return None


def _existing_keys(db) -> set:
    """All r2_keys already in storage_objects so we can skip them."""
    return {row.r2_key for row in db.query(StorageObject.r2_key).all()}


def _insert_object(
    db,
    *,
    user_id: str,
    r2_key: str,
    byte_count: int,
    kind: str,
    uploaded_at: datetime,
    deleted_at: Optional[datetime],
    existing: set,
    dry_run: bool,
) -> bool:
    """Idempotent insert. Returns True if we wrote (or would write in dry-run)."""
    if r2_key in existing:
        return False
    if byte_count <= 0:
        log.warning("skipping %s — non-positive byte count %d", r2_key, byte_count)
        return False
    if dry_run:
        log.info(
            "DRY-RUN would insert: kind=%s key=%s bytes=%d uploaded=%s deleted=%s",
            kind, r2_key, byte_count, uploaded_at.isoformat(),
            deleted_at.isoformat() if deleted_at else None,
        )
    else:
        db.add(StorageObject(
            user_id=user_id,
            r2_key=r2_key,
            bytes=byte_count,
            kind=kind,
            uploaded_at=uploaded_at,
            deleted_at=deleted_at,
        ))
    existing.add(r2_key)
    return True


def backfill_avatars(db, existing: set, dry_run: bool) -> int:
    """Avatars live on UserChannel.avatar_r2_key. We don't store their
    size, so HEAD R2 to find out."""
    rows = db.query(UserChannel).filter(
        UserChannel.avatar_r2_key.is_not(None)
    ).all()
    wrote = 0
    for ch in rows:
        size = _head_r2_size(ch.avatar_r2_key)
        if size is None or size <= 0:
            continue
        # If the channel was soft-deleted, mark the avatar deleted at
        # the same time (we stop billing for soft-deleted channels per
        # the existing meter behavior).
        deleted_at = ch.removed_at
        if _insert_object(
            db,
            user_id=ch.user_id,
            r2_key=ch.avatar_r2_key,
            byte_count=size,
            kind="avatar",
            uploaded_at=ch.added_at,
            deleted_at=deleted_at,
            existing=existing,
            dry_run=dry_run,
        ):
            wrote += 1
    return wrote


def backfill_videos_and_thumbnails(db, existing: set, dry_run: bool) -> tuple:
    """For each UserChannelVideo: backfill the .mp4 (if archived) and
    the thumbnail (if present). Also pick up any caption files we can
    discover from data_json.caption_languages."""
    rows = db.query(UserChannelVideo).all()
    # Channel removal lookup: video objects inherit deleted_at from
    # their parent channel's soft-delete (matches existing meter logic).
    ch_removed: dict = {}
    for ch in db.query(UserChannel).all():
        if ch.removed_at is not None:
            ch_removed[(ch.user_id, ch.channel_id)] = ch.removed_at

    wrote_video = wrote_thumb = wrote_cap = 0
    for v in rows:
        try:
            data = json.loads(v.data_json)
        except (json.JSONDecodeError, TypeError):
            data = {}

        channel_removed_at = ch_removed.get((v.user_id, v.channel_id))

        # Video .mp4
        local_path = data.get("localPath")
        file_size = data.get("fileSizeBytes")
        if isinstance(local_path, str) and local_path and isinstance(file_size, int) and file_size > 0:
            uploaded_at = _parse_iso(data.get("archivedAt")) or v.discovered_at
            if _insert_object(
                db,
                user_id=v.user_id,
                r2_key=local_path,
                byte_count=file_size,
                kind="video",
                uploaded_at=uploaded_at,
                deleted_at=channel_removed_at,
                existing=existing,
                dry_run=dry_run,
            ):
                wrote_video += 1

        # Thumbnail
        if v.thumbnail_r2_key and v.thumbnail_size_bytes and v.thumbnail_size_bytes > 0:
            if _insert_object(
                db,
                user_id=v.user_id,
                r2_key=v.thumbnail_r2_key,
                byte_count=v.thumbnail_size_bytes,
                kind="thumbnail",
                uploaded_at=v.discovered_at,
                deleted_at=channel_removed_at,
                existing=existing,
                dry_run=dry_run,
            ):
                wrote_thumb += 1

        # Caption files (if the FileMeta from the worker recorded them).
        # Shape: data_json["captionLanguages"] = ["en", "es", ...] and
        # the worker writes them at the conventional caption path.
        caption_langs = data.get("captionLanguages")
        if isinstance(caption_langs, list) and local_path:
            # Caption keys follow the worker's convention. Look at the
            # video's R2 key prefix and derive caption paths.
            # Current layout: videos/{video_id}/video.mp4 →
            # videos/{video_id}/captions/{lang}.vtt
            base = local_path.rsplit("/video.mp4", 1)[0] if local_path.endswith("/video.mp4") else None
            if base:
                for lang in caption_langs:
                    if not isinstance(lang, str) or not lang:
                        continue
                    cap_key = f"{base}/captions/{lang}.vtt"
                    size = _head_r2_size(cap_key)
                    if size and size > 0:
                        if _insert_object(
                            db,
                            user_id=v.user_id,
                            r2_key=cap_key,
                            byte_count=size,
                            kind="caption",
                            uploaded_at=_parse_iso(data.get("archivedAt")) or v.discovered_at,
                            deleted_at=channel_removed_at,
                            existing=existing,
                            dry_run=dry_run,
                        ):
                            wrote_cap += 1

    return wrote_video, wrote_thumb, wrote_cap


def backfill_snapshots(db, existing: set, dry_run: bool) -> int:
    """VideoFieldSnapshot rows may have r2_key set (for thumbnail
    snapshots — the others are inline value_json). HEAD R2 for size."""
    rows = db.query(VideoFieldSnapshot).filter(
        VideoFieldSnapshot.r2_key.is_not(None)
    ).all()
    wrote = 0
    for s in rows:
        size = _head_r2_size(s.r2_key)
        if size is None or size <= 0:
            continue
        if _insert_object(
            db,
            user_id=s.user_id,
            r2_key=s.r2_key,
            byte_count=size,
            kind="snapshot",
            uploaded_at=s.captured_at,
            # Snapshots aren't actively deleted today; they live as long
            # as we keep historical thumbnails. Reconciliation will sort
            # out any future drift.
            deleted_at=None,
            existing=existing,
            dry_run=dry_run,
        ):
            wrote += 1
    return wrote


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be inserted without writing.")
    args = parser.parse_args()

    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )

    db = SessionLocal()
    try:
        existing = _existing_keys(db)
        log.info("starting backfill (existing storage_objects rows: %d)", len(existing))

        wrote_avatars = backfill_avatars(db, existing, args.dry_run)
        wrote_v, wrote_t, wrote_c = backfill_videos_and_thumbnails(db, existing, args.dry_run)
        wrote_s = backfill_snapshots(db, existing, args.dry_run)

        if not args.dry_run:
            db.commit()

        total = wrote_avatars + wrote_v + wrote_t + wrote_c + wrote_s
        log.info(
            "%s: avatars=%d videos=%d thumbnails=%d captions=%d snapshots=%d (total=%d)",
            "would write" if args.dry_run else "wrote",
            wrote_avatars, wrote_v, wrote_t, wrote_c, wrote_s, total,
        )
        return 0
    except Exception:
        log.exception("backfill failed")
        db.rollback()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
