"""Monthly archive digest.

Sends each opted-in user one summary of the last 30 days: how many videos
were archived, how many went unavailable, and how much is stored.

"Went unavailable" counts every tracked row whose deletedOnYoutubeAt falls
in the window, whether or not we hold a copy of it - the count is drawn
straight off that timestamp below. It deliberately does not claim the
videos were deleted, or by whom: our detection cannot separate an owner
deleting a video from a takedown, a region block, or our own probe being
refused, so the digest reports only that we stopped being able to see them.

Opt-in: gated on the account-level notifyMonthlyDigest setting, which is
OFF by default. Runs on the 1st of each month via
archive336-monthly-digest.timer.

Usage:
    /opt/aether/venv/bin/python -m scripts.monthly_digest
    /opt/aether/venv/bin/python -m scripts.monthly_digest --dry
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone

from app import notify as notify_lib
from app.db import SessionLocal
from app.models import User, UserChannel, UserChannelVideo

log = logging.getLogger("aether.monthly_digest")

WINDOW_DAYS = 30


def _collect(db, user: User, since: datetime) -> dict:
    """Per-user totals over the window, read from the archived video rows."""
    archived = 0
    deletions = 0
    total_bytes = 0

    channel_ids = [
        cid
        for (cid,) in db.query(UserChannel.channel_id).filter(
            UserChannel.user_id == user.id,
            UserChannel.removed_at.is_(None),
        )
    ]
    if not channel_ids:
        return {"archived": 0, "deletions": 0, "storage_gb": 0.0}

    for row in db.query(UserChannelVideo).filter(
        UserChannelVideo.user_id == user.id,
        UserChannelVideo.channel_id.in_(channel_ids),
    ):
        try:
            d = json.loads(row.data_json) or {}
        except (json.JSONDecodeError, TypeError):
            continue

        size = d.get("fileSizeBytes")
        if isinstance(size, int) and size > 0:
            total_bytes += size

        archived_at = d.get("archivedAt")
        if isinstance(archived_at, str):
            try:
                when = datetime.fromisoformat(archived_at.replace("Z", "+00:00"))
                if when >= since:
                    archived += 1
            except ValueError:
                pass

        deleted_at = d.get("deletedOnYoutubeAt")
        if isinstance(deleted_at, str):
            try:
                when = datetime.fromisoformat(deleted_at.replace("Z", "+00:00"))
                if when >= since:
                    deletions += 1
            except ValueError:
                pass

    return {
        "archived": archived,
        "deletions": deletions,
        "storage_gb": total_bytes / 1_000_000_000,
    }


def main(argv: list[str]) -> int:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )
    dry_run = "--dry" in argv
    since = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)

    db = SessionLocal()
    sent = 0
    skipped = 0
    try:
        for user in db.query(User).all():
            if not notify_lib.user_flag(db, user.id, "notifyMonthlyDigest"):
                skipped += 1
                continue
            stats = _collect(db, user, since)
            if dry_run:
                log.info(
                    "would send digest to %s: %s", user.email, stats
                )
                continue
            ok = notify_lib.notify_monthly_digest(
                db,
                user_id=user.id,
                archived=stats["archived"],
                deletions_caught=stats["deletions"],
                storage_gb=stats["storage_gb"],
            )
            if ok:
                sent += 1
        log.info(
            "monthly digest complete (dry=%s): sent=%d opted_out=%d",
            dry_run, sent, skipped,
        )
        return 0
    except Exception:
        log.exception("monthly digest failed")
        db.rollback()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
