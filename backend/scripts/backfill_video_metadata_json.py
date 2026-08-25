"""One-off: populate Video.metadata_json from the matching legacy
UserChannelVideo.data_json.

Migration added the new shared-pool Video rows without a free-form
metadata blob. The YouTube page renders a lot of fields that live in
the legacy data_json (videoResolution, viewCount, captionLanguages,
tags, etc.). This backfill copies them over so the read-route cutover
can drop UserChannelVideo entirely.

Idempotent: skips Videos whose metadata_json is already set.

Usage:
    /opt/aether/venv/bin/python -m scripts.backfill_video_metadata_json --dry-run
    /opt/aether/venv/bin/python -m scripts.backfill_video_metadata_json
"""
from __future__ import annotations

import logging
import sys

from sqlalchemy import text

from app.db import SessionLocal
from app.models import Video


log = logging.getLogger("aether.backfill_video_metadata_json")


def main(argv: list[str]) -> int:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )
    dry_run = "--dry-run" in argv

    db = SessionLocal()
    try:
        # 1) Add the column if it doesn't exist. SQLAlchemy's
        # Base.metadata.create_all only handles missing tables, not
        # missing columns on existing tables, so we do this ourselves.
        cols = {
            row[1]
            for row in db.execute(text("PRAGMA table_info(videos)")).all()
        }
        if "metadata_json" not in cols:
            log.info("adding column videos.metadata_json")
            if not dry_run:
                db.execute(text("ALTER TABLE videos ADD COLUMN metadata_json TEXT"))
                db.commit()
        else:
            log.info("column videos.metadata_json already exists")

        # Index UserChannelVideo.data_json by youtube_id (video_id col).
        # We use raw SQL because the legacy model class may go away
        # before this script does; binding through ORM here would tie
        # the script's lifetime to the model class.
        legacy_rows = db.execute(
            text("SELECT video_id, data_json FROM user_channel_videos")
        ).all()
        legacy_by_yt: dict[str, str] = {}
        for video_id, data_json in legacy_rows:
            if video_id and data_json:
                # If multiple users archived the same video, all their
                # data_json blobs are equivalent for our purposes (same
                # YouTube source), so first-wins is fine.
                legacy_by_yt.setdefault(video_id, data_json)
        log.info("indexed %d legacy data_json blobs", len(legacy_by_yt))

        updated = 0
        skipped_existing = 0
        skipped_no_legacy = 0
        for video in db.query(Video).all():
            if video.metadata_json:
                skipped_existing += 1
                continue
            blob = legacy_by_yt.get(video.youtube_id)
            if not blob:
                skipped_no_legacy += 1
                continue
            video.metadata_json = blob
            updated += 1
            log.info(
                "  %s metadata_json <- %d bytes",
                video.youtube_id,
                len(blob),
            )

        log.info(
            "summary: updated=%d skipped_existing=%d skipped_no_legacy=%d",
            updated,
            skipped_existing,
            skipped_no_legacy,
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
