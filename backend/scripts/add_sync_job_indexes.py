"""Add the sync_jobs uniqueness + claim indexes to an existing database.

There is no Alembic here, and Base.metadata.create_all only creates
MISSING TABLES - it will not add an index to a table that already exists.
So this runs once, by hand, like the other one-off migrations in this
directory.

Two indexes:

  uniq_sync_jobs_active  (user_id, video_id, kind) WHERE status active
      Makes "queued exactly once" a property of the database instead of
      four call sites each remembering to read-then-write. The sweep runs
      in a separate process from the API, so its snapshot goes stale
      between the read and the commit; the loser of that race must write
      nothing rather than download the same video twice and bill the
      storage twice. Partial on purpose - terminal rows accumulate and the
      give-up counter reads them.

  ix_sync_jobs_claim  (user_id, status, kind, created_at)
      The claim query filters those three and takes the oldest. Without
      this it used the single-column status index and sorted every pending
      row, which is fine at ten and not at a 20,000-video catalogue.

Existing duplicates are collapsed BEFORE the unique index is created,
newest kept, or the CREATE would fail on any database that already has
some. Duplicates are marked done rather than deleted - nothing here
destroys rows.

Usage:
    /opt/aether/venv/bin/python -m scripts.add_sync_job_indexes [--dry]
"""
from __future__ import annotations

import logging
import sys

from sqlalchemy import text

from app.db import SessionLocal

log = logging.getLogger("aether.add_sync_job_indexes")

_FIND_DUPES = text(
    """
    SELECT id FROM (
      SELECT id, ROW_NUMBER() OVER (
        PARTITION BY user_id, video_id, kind
        ORDER BY created_at DESC, id DESC
      ) AS rn
      FROM sync_jobs
      WHERE status IN ('pending','running')
    ) WHERE rn > 1
    """
)


def main(argv: list[str]) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )
    dry = "--dry" in argv
    db = SessionLocal()
    try:
        dupe_ids = [r[0] for r in db.execute(_FIND_DUPES)]
        log.info("duplicate active jobs to collapse: %d", len(dupe_ids))
        if dupe_ids and not dry:
            db.execute(
                text(
                    "UPDATE sync_jobs SET status='done', "
                    "finished_at=CURRENT_TIMESTAMP "
                    "WHERE id IN :ids"
                ).bindparams(ids=tuple(dupe_ids))
            )

        for name, ddl in (
            (
                "uniq_sync_jobs_active",
                "CREATE UNIQUE INDEX IF NOT EXISTS uniq_sync_jobs_active "
                "ON sync_jobs (user_id, video_id, kind) "
                "WHERE status IN ('pending','running')",
            ),
            (
                "ix_sync_jobs_claim",
                "CREATE INDEX IF NOT EXISTS ix_sync_jobs_claim "
                "ON sync_jobs (user_id, status, kind, created_at)",
            ),
        ):
            if dry:
                log.info("would create %s", name)
                continue
            db.execute(text(ddl))
            log.info("created %s", name)

        if not dry:
            db.commit()
        log.info("done (dry=%s)", dry)
        return 0
    except Exception:
        log.exception("migration failed")
        db.rollback()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
