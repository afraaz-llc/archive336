"""Auto-download safety net.

The PubSubHubbub upload notification queues a download the moment YouTube
tells us about a new video. This sweep is the backstop for everything that
path can miss: an expired hub lease, a notification that never arrived, a
window where we were down, or a job that was created but later failed and
left the video unarchived.

For every channel with "Automatically sync" on (and an active plan), it
queues any new upload that still has no archived file and no in-flight job.
Scoped to videos published AFTER the channel was added - the pre-existing
back catalogue stays an explicit, user-triggered Sync so nobody gets a
surprise storage bill for adding a channel.

Idempotent: enqueue_downloads skips anything already archived or already
pending/running, so running this repeatedly is a no-op once things are
caught up.

Usage:
    /opt/aether/venv/bin/python -m scripts.auto_download_sweep
    /opt/aether/venv/bin/python -m scripts.auto_download_sweep --dry
"""

from __future__ import annotations

import logging
import sys

from app import auto_download
from app.db import SessionLocal
from app.models import UserChannel
from app.service_access import active_service_user_ids

log = logging.getLogger("aether.auto_download_sweep")


def main(argv: list[str]) -> int:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )
    dry_run = "--dry" in argv

    db = SessionLocal()
    totals = {"channels": 0, "eligible": 0, "queued": 0}
    try:
        # Paused accounts spend nothing. A failed card pauses backups (the
        # owner's call), so this uses the same predicate as the HTTP gate -
        # see app/service_access.py. Existing archives are untouched; only
        # NEW work stops, so restoring service is just the column flipping
        # back.
        entitled = active_service_user_ids(db)
        rows = (
            db.query(UserChannel)
            .filter(
                UserChannel.removed_at.is_(None),
                UserChannel.user_id.in_(entitled),
            )
            .all()
        )
        totals["channels"] = len(rows)
        for uc in rows:
            if not auto_download.auto_download_enabled(db, uc):
                continue
            totals["eligible"] += 1
            try:
                pending = auto_download.pending_new_uploads(db, uc)
                if not pending:
                    continue
                if dry_run:
                    log.info(
                        "user %s channel %s: %d new upload(s) would be queued",
                        uc.user_id, uc.channel_id, len(pending),
                    )
                    continue
                created = auto_download.enqueue_downloads(
                    db,
                    user_id=uc.user_id,
                    channel_youtube_id=uc.channel_id,
                    video_ids=pending,
                )
                if created:
                    db.commit()
                    totals["queued"] += created
                    log.info(
                        "user %s channel %s: queued %d download(s)",
                        uc.user_id, uc.channel_id, created,
                    )
            except Exception:
                db.rollback()
                log.exception(
                    "sweep failed for user %s channel %s",
                    uc.user_id, uc.channel_id,
                )

        log.info(
            "auto-download sweep complete (dry=%s): channels=%d eligible=%d queued=%d",
            dry_run, totals["channels"], totals["eligible"], totals["queued"],
        )
        return 0
    except Exception:
        log.exception("auto-download sweep failed")
        db.rollback()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
