"""One-off: backfill Video.synced_at + Video.bytes_stored from the
ground-truth storage_objects table.

Found after the initial migration that:
  - Video.synced_at was sourced from data_json.archivedAt, which can
    correspond to a re-upload artifact rather than the canonical
    storage object. Should use the EARLIEST storage_objects.uploaded_at
    matching the youtube_id - that's when bytes first hit R2 and
    when our R2 bill starts.
  - Video.bytes_stored was the video file's size only. The matching
    thumbnail in storage_objects is also real R2 storage we pay for;
    fold those bytes into bytes_stored so the v2 billing model
    matches v1 within tolerance.

Idempotent. Re-running on an already-fixed Video is a no-op.

Usage:
    /opt/aether/venv/bin/python -m scripts.fix_video_sync_state --dry-run
    /opt/aether/venv/bin/python -m scripts.fix_video_sync_state
"""
from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

from app.db import SessionLocal
from app.models import Video


log = logging.getLogger("aether.fix_video_sync_state")


_R2_KEY_VIDEO_ID = re.compile(r"^videos/([A-Za-z0-9_-]+)/video\.mp4$")
_R2_KEY_THUMBNAIL_ID = re.compile(r"^thumbnails/([A-Za-z0-9_-]+)\.jpg$")


def _parse_dt(s) -> Optional[datetime]:
    if s is None:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
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
        # Index storage_objects per youtube_id, separately for videos
        # and thumbnails. For videos, keep the earliest non-deleted
        # uploaded_at - that's when R2 storage starts being billed.
        video_earliest: dict[str, datetime] = {}
        video_bytes: dict[str, int] = {}
        thumb_bytes: dict[str, int] = {}

        rows = db.execute(
            text(
                "SELECT r2_key, bytes, uploaded_at, kind FROM storage_objects "
                "WHERE deleted_at IS NULL"
            )
        ).all()
        for r2_key, byts, uploaded_at, kind in rows:
            dt = _parse_dt(uploaded_at)
            if kind == "video":
                m = _R2_KEY_VIDEO_ID.match(r2_key)
                if m is None:
                    continue  # legacy/non-canonical path
                yt = m.group(1)
                if dt is not None and (
                    yt not in video_earliest or dt < video_earliest[yt]
                ):
                    video_earliest[yt] = dt
                # bytes_stored uses the canonical (videos/<id>/video.mp4)
                # row's size; non-canonical artifacts are billing trash.
                if yt not in video_bytes:
                    video_bytes[yt] = byts
            elif kind == "thumbnail":
                m = _R2_KEY_THUMBNAIL_ID.match(r2_key)
                if m is None:
                    continue
                yt = m.group(1)
                thumb_bytes[yt] = byts

        # Walk Video rows, set the right values.
        updates = 0
        for video in db.query(Video).all():
            yt = video.youtube_id
            new_synced = video_earliest.get(yt)
            new_bytes = (
                (video_bytes.get(yt) or 0) + (thumb_bytes.get(yt) or 0)
            ) or None

            current_synced = (
                video.synced_at.replace(tzinfo=timezone.utc)
                if video.synced_at and video.synced_at.tzinfo is None
                else video.synced_at
            )
            changed = False
            if new_synced is not None and current_synced != new_synced:
                log.info(
                    "  %s synced_at: %s -> %s",
                    yt,
                    current_synced,
                    new_synced,
                )
                video.synced_at = new_synced
                changed = True
            if new_bytes is not None and video.bytes_stored != new_bytes:
                log.info(
                    "  %s bytes_stored: %s -> %s",
                    yt,
                    video.bytes_stored,
                    new_bytes,
                )
                video.bytes_stored = new_bytes
                changed = True
            if changed:
                updates += 1

        log.info("updates=%d", updates)
        if dry_run:
            log.info("dry-run: rolling back")
            db.rollback()
        else:
            db.commit()
            log.info("committed.")
        return 0
    except Exception:
        log.exception("fix failed")
        db.rollback()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
