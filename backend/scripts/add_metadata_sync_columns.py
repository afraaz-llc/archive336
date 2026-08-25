"""One-off migration: add metadata-sync + thumbnail-dedup columns to
user_channel_videos.

The new VideoFieldSnapshot table is auto-created by SQLAlchemy's
create_all() on boot, but ALTER TABLE ADD COLUMN on an existing table
needs to be done explicitly. This script adds the columns when they're
missing - idempotent, safe to re-run.

Columns added:
  - last_metadata_sync_at: when this video's metadata was last
    confirmed against YouTube.
  - thumbnail_sha256: content hash of the current archived thumbnail
    bytes (authoritative "is this a new image" check on rescan).
  - thumbnail_etag: HEAD etag of the current thumbnail URL (cheap
    pre-check before downloading bytes).
  - thumbnail_content_length: HEAD content-length of the current
    thumbnail URL (secondary cheap pre-check).

Usage:
    /opt/aether/venv/bin/python -m scripts.add_metadata_sync_columns
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app.db import engine  # noqa: E402


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


def column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(r[1] == column for r in rows)


_COLUMNS = [
    ("last_metadata_sync_at", "DATETIME"),
    ("thumbnail_sha256", "VARCHAR"),
    ("thumbnail_etag", "VARCHAR"),
    ("thumbnail_content_length", "INTEGER"),
    ("last_comments_sync_at", "DATETIME"),
]


def main() -> None:
    with engine.begin() as conn:
        for name, ddl_type in _COLUMNS:
            if column_exists(conn, "user_channel_videos", name):
                log.info("%s already present - skipping", name)
                continue
            log.info("adding %s (%s) to user_channel_videos", name, ddl_type)
            conn.execute(
                text(
                    "ALTER TABLE user_channel_videos "
                    f"ADD COLUMN {name} {ddl_type}"
                )
            )
        log.info("done")


if __name__ == "__main__":
    main()
