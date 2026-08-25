"""Recorder for R2 operations against the per-user billing ledger.

The ops-side sibling of ``storage_ledger.py``. Where storage_ledger.py
manages StorageObject rows (one per object lifecycle), this module
manages R2OperationLog rows (one per (subject, bucket, op_class, day)
daily counter).

Why a separate module: r2.py is concerned with talking to S3, not
billing. ops_ledger.py is concerned with translating "we just did
operation X for subject Y on bucket Z" into a durable counter row.
r2.py calls record_op(); ops_ledger.py handles the UPSERT.

Phase A ships the recorder. Phase B (next) wires r2.py call sites to
actually call record_op() with the right subject + op_class. Until
Phase B lands, this module is dead code — that's intentional, so we
can verify the schema + recorder work end-to-end before changing
r2.py behavior.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import R2_OPS_PLATFORM_SUBJECT, R2OperationLog


log = logging.getLogger("archive336.ops_ledger")

# Re-export the sentinel so call sites can import it from one place.
PLATFORM = R2_OPS_PLATFORM_SUBJECT

# The buckets we use. USER_CONTENT_BUCKET tracks whatever object store r2.py
# is pointed at - Backblaze B2 (aether-archive-prod) since the storage
# migration, with the legacy R2 name as fallback - so ops recorded by r2.py
# land on a known bucket instead of warning. BACKUPS_BUCKET (Litestream) stays
# on R2. Ops on any other bucket still persist (subject == caller's user_id)
# but log a warning.
USER_CONTENT_BUCKET = (
    os.environ.get("STORAGE_BUCKET")
    or os.environ.get("R2_BUCKET")
    or "aether-archive-tool"
)
BACKUPS_BUCKET = "aether-archive-backups"
KNOWN_BUCKETS = frozenset({USER_CONTENT_BUCKET, BACKUPS_BUCKET})


def _today_utc() -> datetime:
    """Day-aligned UTC timestamp for the current day (00:00:00 UTC)."""
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def record_op(
    db: Session,
    *,
    subject: str,
    bucket: str,
    op_class: str,
    count: int = 1,
) -> None:
    """Increment the daily counter for (subject, bucket, op_class).

    Uses SQLite's INSERT ... ON CONFLICT DO UPDATE so concurrent
    workers can both call this without losing increments — the row's
    ``count`` grows monotonically through the day. If there's no row
    yet for today, one is created with count = `count`.

    Args:
        db: SQLAlchemy session. Caller is responsible for committing
            in their own transaction boundary, OR we commit here if
            the caller hasn't started a transaction. We err on the
            side of committing so a forgotten commit doesn't cost us
            billing data.
        subject: A real users.id (36-char UUID) OR ``PLATFORM`` for
            Litestream / cron / reconcile / any platform-fixed op.
        bucket: Bucket name, e.g. ``"aether-archive-tool"``. Free-form
            string — unknown buckets log a warning but are still
            recorded (so we never silently lose ops data).
        op_class: ``"A"`` (writes, lists, multipart) or ``"B"``
            (reads, heads). See docs/CLOUDFLARE_AUDIT.md §2 for the
            full S3-API → class mapping. Free ops (DeleteObject,
            AbortMultipartUpload) should NOT be recorded — they
            don't show up on Cloudflare's invoice and recording them
            here would over-bill users.
        count: Increment amount. Defaults to 1 (one R2 call). Use a
            larger value for operations that batch multiple billable
            sub-ops (e.g., a paginated list that did 5 LIST calls).

    Best-effort: a failure here logs an exception and returns without
    raising. We never want billing instrumentation to take down a real
    R2 operation. Reconciliation will catch any drift.
    """
    if op_class not in ("A", "B"):
        log.error(
            "record_op called with invalid op_class=%r (must be 'A' or 'B'); "
            "subject=%s bucket=%s count=%d", op_class, subject, bucket, count,
        )
        return
    if count <= 0:
        return
    if bucket not in KNOWN_BUCKETS:
        log.warning(
            "record_op called with unknown bucket=%r; recording anyway "
            "(subject=%s op_class=%s count=%d)",
            bucket, subject, op_class, count,
        )

    today = _today_utc()
    now = datetime.now(timezone.utc)

    try:
        stmt = sqlite_insert(R2OperationLog).values(
            id=str(uuid.uuid4()),
            subject=subject,
            bucket=bucket,
            op_class=op_class,
            day=today,
            count=count,
            recorded_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["subject", "bucket", "op_class", "day"],
            set_={
                "count": R2OperationLog.count + stmt.excluded.count,
                "recorded_at": stmt.excluded.recorded_at,
            },
        )
        db.execute(stmt)
        db.commit()
    except Exception:
        # Roll back so the caller's outer transaction isn't poisoned.
        # Then swallow — billing instrumentation must never break the
        # actual R2 call path. Reconciliation against Cloudflare's
        # per-bucket totals will surface persistent drift.
        try:
            db.rollback()
        except Exception:
            pass
        log.exception(
            "record_op failed: subject=%s bucket=%s op_class=%s count=%d",
            subject, bucket, op_class, count,
        )
