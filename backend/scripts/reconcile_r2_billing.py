"""Monthly storage reconciliation - compare our billing ledger to the
object store's actual bytes-on-disk.

For the previous calendar month, sums our active StorageObject rows and
compares them to what the user-content bucket actually holds. Since the
storage migration that bucket is on Backblaze B2 (invisible to Cloudflare's
R2 GraphQL), so we read its real size via the S3 API (r2.bucket_stats) rather
than CF analytics. Drift between the two is the signal we'd notice if our
per-user bills don't add up to the real storage bill.

Runs monthly on the 4th — one day after the bill cron's 3rd-of-month
run — so any drift surfaces before users see the next invoice cycle.

Logs every run to ``ReconciliationLog`` with one of:

  r2_billing_drift_storage  — main per-period summary row, always logged
                              regardless of drift size. ``alerted=true``
                              when drift exceeds the alert threshold.

The reconciliation is platform-level, not per-user — ``user_id`` is
NULL on these rows. Per-user attribution is a Phase G concern; right
now we just need to know if the TOTAL matches.

Known sources of expected drift (see docs/CLOUDFLARE_AUDIT.md §14 and
the storage billing design doc):

  - Hour-boundary alignment: Cloudflare samples bytes-on-disk hourly,
    we integrate continuously. An object uploaded at HH:30 contributes
    a full hour in our model but half an hour in CF's (it wasn't on
    disk during the HH:00 sample, only during HH+1:00).

  - Month-length convention: we use the average month (730.485 hours)
    for the rate but integrate over the actual calendar month. Net
    effect is small (~0.05% on a 31-day month, the opposite sign on
    a 30-day month, zero on average).

  - Metadata overhead estimate: our 80-byte object-overhead constant
    is an estimate; CF's metadataSize is exact.

  - Free tier: CF gives 10 GB-mo of free storage; we ignore that at
    the user level (it's platform margin). Expect our number to be
    consistently ~10 GB-mo HIGHER than CF's billable number until
    we exceed 10 GB-mo of total storage, at which point the gap
    closes.

  - Backups bucket: our ledger doesn't track Litestream backups, but
    we filter the CF side to just the user-content bucket so this
    doesn't show up as drift.

Usage:
    /opt/aether/venv/bin/python -m scripts.reconcile_r2_billing
    /opt/aether/venv/bin/python -m scripts.reconcile_r2_billing --dry-run
    /opt/aether/venv/bin/python -m scripts.reconcile_r2_billing --month 2026-04
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from app import billing as billing_lib
from app import cloudflare
from app.db import SessionLocal
from app.models import ReconciliationLog


log = logging.getLogger("aether.reconcile_r2_billing")


# Alert thresholds. Drift below both is logged but not alerted —
# the steady-state expectation given the known structural sources
# of drift above is single-digit percent and a few cents absolute.
# We trip when EITHER threshold is exceeded:
#   - relative drift bigger than this means something structural shifted
#   - absolute drift bigger than this means real money is at stake
# Tune as we accumulate baseline data.
DRIFT_ALERT_PCT = 5.0
DRIFT_ALERT_USD = 1.00


def _previous_calendar_month(today: datetime) -> tuple[datetime, datetime, str]:
    """Return [start, end) of the calendar month BEFORE ``today``, plus a
    ``YYYY-MM`` label.

    Cloudflare bills on calendar boundaries, so reconciliation always
    anchors to the previous complete UTC month. Run on day 4: covers
    last month's invoice.
    """
    first_of_this_month = today.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    # Step back to land in the previous month, then snap to its 1st.
    prev_month_start = (first_of_this_month - timedelta(seconds=1)).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    label = prev_month_start.strftime("%Y-%m")
    return prev_month_start, first_of_this_month, label


def _parse_month_arg(s: str) -> tuple[datetime, datetime, str]:
    """``YYYY-MM`` → ([start, end), label) for that calendar month."""
    year, month = map(int, s.split("-"))
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    # First of next month.
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return start, end, s


def reconcile(
    period_start: datetime,
    period_end: datetime,
    label: str,
    dry_run: bool = False,
) -> dict:
    """Run one reconciliation pass over [period_start, period_end).

    Snapshot-based comparison: pull the CURRENT bytes-on-disk from
    Cloudflare's analytics + sum our active StorageObject rows. For
    write-once archival data (our case) these should agree closely
    at any single moment. Then approximate the period's CF cost by
    treating the snapshot as constant for the whole period - good
    enough at our scale and ~10× simpler than time-weighted
    integration over CF's adaptive samples.

    Returns a dict with the comparison numbers. Always logs a
    ReconciliationLog row unless ``dry_run``.
    """
    user_bucket = (
        os.environ.get("STORAGE_BUCKET")
        or os.environ.get("R2_BUCKET")
        or "aether-archive-tool"
    )
    from app import ops_ledger, r2  # noqa: WPS433 (lazy to avoid import cycle)
    from app.models import StorageObject  # noqa: WPS433

    # --- Actual bytes-on-disk snapshot ---
    # The user-content bucket is on Backblaze B2 now (invisible to Cloudflare's
    # R2 GraphQL), so list it directly via the S3 API. The cf_* names are kept
    # for the ReconciliationLog/return shape; they mean "actual store" (B2).
    stats = r2.bucket_stats(subject=ops_ledger.PLATFORM)
    if stats is None:
        log.warning(
            "b2 storage snapshot unavailable for %s - skipping",
            label,
        )
        return {"label": label, "status": "skipped_no_storage_data"}
    cf_bytes = stats["bytes"]

    # --- Our ledger snapshot (active StorageObject rows) ---
    db = SessionLocal()
    try:
        rows = (
            db.query(StorageObject)
            .filter(StorageObject.deleted_at.is_(None))
            .all()
        )
        ours_bytes = sum(r.bytes + r.metadata_bytes for r in rows)
    finally:
        db.close()

    # --- Estimate the period's storage cost on both sides ---
    # Treat the snapshot as if it held constant for the whole period.
    # For write-once data this is accurate within a few percent;
    # short-lived storage events that came and went mid-period get
    # missed by both sides equally, so the drift signal is still valid.
    period_hours = (period_end - period_start).total_seconds() / 3600.0
    cf_cost = cf_bytes * period_hours * billing_lib.STORAGE_COST_USD_PER_BYTE_HOUR
    ours_cost = ours_bytes * period_hours * billing_lib.STORAGE_COST_USD_PER_BYTE_HOUR

    drift_bytes = ours_bytes - cf_bytes
    drift_pct = (drift_bytes / cf_bytes * 100.0) if cf_bytes > 0 else (
        0.0 if ours_bytes == 0 else 999.0
    )
    drift_usd = ours_cost - cf_cost
    alerted = abs(drift_pct) >= DRIFT_ALERT_PCT or abs(drift_usd) >= DRIFT_ALERT_USD

    log.info(
        "r2 billing reconciliation %s: ours_bytes=%d cf_bytes=%d "
        "ours_cost=$%.4f cf_cost=$%.4f drift=%+.2f%% (%+.4f USD) alert=%s",
        label,
        ours_bytes,
        cf_bytes,
        ours_cost,
        cf_cost,
        drift_pct,
        drift_usd,
        alerted,
    )

    details = {
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "label": label,
        "user_bucket": user_bucket,
        "ours_bytes": ours_bytes,
        "cf_bytes": cf_bytes,
        "ours_cost_usd": ours_cost,
        "cf_cost_usd": cf_cost,
        "drift_pct": drift_pct,
        "drift_usd": drift_usd,
        "alert_threshold_pct": DRIFT_ALERT_PCT,
        "alert_threshold_usd": DRIFT_ALERT_USD,
    }

    if not dry_run:
        db = SessionLocal()
        try:
            db.add(
                ReconciliationLog(
                    user_id=None,
                    action="r2_billing_drift_storage",
                    r2_key=None,
                    details_json=json.dumps(details),
                    ran_at=datetime.now(timezone.utc),
                    alerted=alerted,
                )
            )
            db.commit()
        finally:
            db.close()

    return {
        "label": label,
        "status": "alerted" if alerted else "ok",
        **details,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report only; don't write a ReconciliationLog row.",
    )
    parser.add_argument(
        "--month",
        type=str,
        default=None,
        help="Calendar month to reconcile (YYYY-MM). Defaults to last month.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )

    if args.month:
        period_start, period_end, label = _parse_month_arg(args.month)
    else:
        today = datetime.now(timezone.utc)
        period_start, period_end, label = _previous_calendar_month(today)

    result = reconcile(period_start, period_end, label, dry_run=args.dry_run)
    log.info("reconciliation done: %s", result["status"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
