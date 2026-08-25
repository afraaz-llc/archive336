"""One-off: clear Video.bytes_stored for videos we no longer actually store.

The shared `videos` table is keyed by channel_id, not user_id. When a user
is deleted, confirm_account_deletion purges their Backblaze objects and
flips storage_objects.deleted_at, then cascades every user-FK child table -
but a Video row has no user FK, so it survives carrying the deleted user's
r2_key and the bytes_stored value from when the file really existed.

Nothing decrements Video.bytes_stored when its backing storage disappears,
so those bytes become phantom: counted by billing (compute_user_byte_hours_v2
sums bytes_stored with no join to storage_objects) and by the channel-card
cost, for bytes no longer stored. On the one affected channel today that is
1.54 GB of phantom over 0.85 GB real - a ~2.8x storage overbill, latent only
because the accrual sits under the $5 invoice floor.

This returns each phantom row to the schema's "tracked, not downloaded"
state: bytes_stored / r2_key / synced_at all NULL. It NEVER touches a video
that has a live storage object, which is the safety property that matters.

Idempotent - a second run finds nothing. DB-only, no bucket calls.

Usage:
    /opt/aether/venv/bin/python -m scripts.backfill_phantom_video_bytes --dry-run
    /opt/aether/venv/bin/python -m scripts.backfill_phantom_video_bytes
"""
from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import text

from app.db import SessionLocal


log = logging.getLogger("aether.backfill_phantom_video_bytes")


# A phantom row: bytes_stored is set, but no LIVE storage object backs its
# r2_key. Keyed on exact r2_key equality so it is scheme-agnostic (works for
# both videos/<id>/video.mp4 and the legacy users/<uid>/videos/<id>/... keys).
# NOT EXISTS is also true when r2_key IS NULL, catching bytes-without-key
# rows. It can never flag a genuinely-stored video, because a stored video
# has exactly one matching storage_objects row with deleted_at IS NULL.
_PHANTOM_SELECT = text(
    """
    SELECT v.id, v.youtube_id, v.bytes_stored, v.r2_key
    FROM videos v
    WHERE v.bytes_stored IS NOT NULL
      AND v.bytes_stored > 0
      AND NOT EXISTS (
            SELECT 1 FROM storage_objects s
            WHERE s.r2_key = v.r2_key
              AND s.deleted_at IS NULL
          )
    """
)

_CLEAR = text(
    """
    UPDATE videos
    SET bytes_stored = NULL, r2_key = NULL, synced_at = NULL
    WHERE id = :id
    """
)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Clear Video.bytes_stored for videos with no live storage."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would change and roll back without writing.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s"
    )

    db = SessionLocal()
    try:
        rows = db.execute(_PHANTOM_SELECT).fetchall()
        log.info(
            "phantom scan: %d video row(s) with bytes_stored but no live storage (dry=%s)",
            len(rows),
            args.dry_run,
        )
        total_bytes = 0
        for r in rows:
            total_bytes += int(r.bytes_stored or 0)
            log.info(
                "  %s: bytes_stored=%d r2_key=%s -> NULL",
                r.youtube_id,
                r.bytes_stored,
                r.r2_key,
            )
            if not args.dry_run:
                db.execute(_CLEAR, {"id": r.id})

        if args.dry_run:
            db.rollback()
            log.info(
                "dry run: rolled back. %d row(s), %.3f GB of phantom bytes would clear",
                len(rows),
                total_bytes / 1e9,
            )
        else:
            db.commit()
            log.info(
                "committed: cleared %d row(s), %.3f GB of phantom bytes",
                len(rows),
                total_bytes / 1e9,
            )
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
