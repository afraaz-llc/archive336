"""Daily storage reconciliation - keep the ledger honest about what's in the bucket.

For every bucket object that no StorageObject row knows about, and
every row we're billing for that has no matching object, take action:

  ORPHAN   (bucket has it, NO StorageObject row references it at all -
           not even a soft-deleted one): report it, and delete it from
           the bucket ONLY when the run was given --delete-orphans. A
           default run never deletes. We're paying for bytes we never
           billed for. Could be a failed multipart upload, a
           pre-Phase-A leftover, or a missed event-log insert.
           Dropping it is safe in principle - nobody has a DB record
           of it - but it is the one action here that destroys user
           data with no product-level undo, so it is opt-in and
           brake-limited (see the delete brake below).

  PHANTOM  (a LIVE ledger row says yes, bucket says no): mark
           deleted_at=now and ALERT. Means we're billing a user for
           bytes that don't exist. Almost always a data loss event we
           need to know about; never silently absorb.

  DRIFT    (live row + object, bytes differ): update DB to the
           bucket's bytes and ALERT. The bucket is the source of
           money so we trust its number. Should never happen in
           normal ops.

SOFT-DELETE CONTRACT - read this before touching the orphan query:

  ``deleted_at IS NOT NULL`` does NOT mean "this file is gone". It
  means "stop billing for it". storage_ledger.propagate_channel_soft_delete
  flips deleted_at on every row of a channel the moment a user removes
  it, while the real objects deliberately stay in the bucket for the
  entire 30-day grace window in which the UI promises a restore. We
  eat that storage cost on purpose. scripts/purge_removed.py is the
  ONLY job allowed to drop those bytes, and only once the window has
  expired.

  So orphan detection diffs the bucket against EVERY StorageObject
  row, live or soft-deleted. A soft-deleted row is proof we know
  exactly what that object is and are keeping it deliberately.
  Narrowing that query back to ``deleted_at IS NULL`` turns every
  removed channel's files into "orphans" and permanently destroys
  them mid-grace-window. That is exactly what this job did before
  2026-07-21, and why its timer was disabled on production.

  The live set (``deleted_at IS NULL``) is still the correct set for
  the billing-shaped checks. Drift, phantoms and the metadata
  backfill all answer "is what we're charging for correct?", so they
  only ever look at rows we're actually charging for.

Single pass over the whole bucket. Scales to millions of objects on
SQLite; bumping to Postgres if/when we need parallel per-user passes
is a Phase F concern.

Usage:
    # report only, touches nothing:
    /opt/aether/venv/bin/python -m scripts.reconcile --dry-run

    # apply ledger fixes (phantoms, drift, metadata) but only REPORT
    # orphans - this is the safe default and what the timer runs:
    /opt/aether/venv/bin/python -m scripts.reconcile

    # additionally hard-delete orphans from the bucket:
    /opt/aether/venv/bin/python -m scripts.reconcile --delete-orphans

See docs/STORAGE_BILLING_DESIGN.md for the full design.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Dict, Set

from app import r2
from app.db import SessionLocal
from app.models import ReconciliationLog, StorageObject, Video


log = logging.getLogger("aether.reconcile")


# --- Delete brake ---------------------------------------------------
# Orphan deletion is the only irreversible thing this job does, and
# there is no product-level undo for it. Every plausible way this
# script goes wrong (a bad query, an empty DB after a restore, a
# half-migrated key layout, a bucket pointed at the wrong env) shows
# up the same way: a sudden run where a LOT of real files look like
# orphans. So we refuse to delete when the world looks unexpected and
# make someone read the log instead.
#
# True orphans arrive in ones and twos - a failed multipart upload, a
# crash between the PUT and the ledger insert. A run proposing dozens
# is not a cleanup, it is a bug.
#
# FLOOR keeps a tiny bucket usable: at today's ~18 objects, 5% rounds
# to zero and the fraction rule alone would block every legitimate
# cleanup forever. HARD_CAP keeps a huge bucket from ever handing one
# run a five-figure delete list.
ORPHAN_DELETE_FLOOR = 10        # always allow at least this many
ORPHAN_DELETE_FRACTION = 0.05   # ...or 5% of the bucket, whichever is larger
ORPHAN_DELETE_HARD_CAP = 50     # ...but never more than this in one run


def _orphan_delete_allowance(bucket_object_count: int) -> int:
    """How many orphans this run is permitted to delete. See the brake
    constants above for why it is shaped this way."""
    scaled = int(bucket_object_count * ORPHAN_DELETE_FRACTION)
    return min(ORPHAN_DELETE_HARD_CAP, max(ORPHAN_DELETE_FLOOR, scaled))


def _walk_bucket() -> Dict[str, int]:
    """Full bucket scan. Returns {r2_key: bytes}. Empty dict if R2 isn't configured.

    Phase F lockdown: also asserts every object is on the ``STANDARD``
    storage class. Any object in Infrequent Access is logged as a
    reconciliation event and counted in the run summary — IA has no
    free tier on ops and 2× rate, see docs/CLOUDFLARE_AUDIT.md §4.4
    and the $9 horror case. Should never happen in normal ops since
    we never call ``PutObject`` with a non-Standard class, but the
    assertion catches accidental dashboard misclicks before they
    compound across many objects.
    """
    client = r2.client()
    bucket = r2.bucket()
    if client is None or bucket is None:
        log.warning("R2 not configured; nothing to reconcile against")
        return {}

    objects: Dict[str, int] = {}
    non_standard: list[tuple[str, str]] = []  # (key, storage_class)
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            objects[obj["Key"]] = int(obj.get("Size") or 0)
            # StorageClass may be absent on legacy objects; treat
            # missing as Standard (the default class). Anything
            # explicit-non-Standard is the failure case we care about.
            sc = (obj.get("StorageClass") or "STANDARD").upper()
            if sc != "STANDARD":
                non_standard.append((obj["Key"], sc))

    if non_standard:
        # Loud alert — every entry here is potential surprise cost.
        log.error(
            "F1 lockdown VIOLATION: %d non-Standard storage-class objects found "
            "(IA has no free tier on ops, 2x rate). Review and migrate ALL "
            "objects back to STANDARD via the Cloudflare dashboard. First 10:",
            len(non_standard),
        )
        for k, sc in non_standard[:10]:
            log.error("  %s  -> %s", k, sc)
    return objects


def _user_id_for_key(r2_key: str) -> str:
    """Derive user_id from a new-layout key, or empty string for legacy keys.

    New layout: users/{user_id}/... → returns user_id.
    Legacy layout: returns empty (no per-user grouping in the key).
    """
    if r2_key.startswith("users/"):
        parts = r2_key.split("/", 2)
        if len(parts) >= 2:
            return parts[1]
    return ""


def _check_all_buckets_storage_class() -> None:
    """Phase F lockdown via Cloudflare's authoritative storage metrics.

    Replaces the boto3-list approach (which can't see the Litestream
    backups bucket because those credentials are correctly scoped to
    put/get only — no list permission). Uses the GraphQL
    ``r2StorageAdaptiveGroups`` dataset to enumerate every bucket in
    the account broken out by storage class, then asserts every
    bucket has zero non-Standard objects.

    Why this is the right F1: a single Cloudflare-authoritative query
    that covers both ``aether-archive-tool`` AND
    ``aether-archive-backups`` (and anything else that might appear),
    with no S3-side permission required, no extra Class A ops issued
    against either bucket.

    Skips silently if the account-analytics token isn't configured
    (typical for local dev). Treats query failure as "skip this run"
    rather than a violation — we'd rather under-alert than alert
    falsely.
    """
    from app import cloudflare as cf

    storage = cf.r2_storage_by_bucket_and_class()
    if storage is None:
        log.info(
            "F1 storage-class check skipped: account-analytics token "
            "not configured or query failed"
        )
        return

    violations: list[tuple[str, str, int]] = []  # (bucket, class, count)
    for bucket_name, by_class in storage.items():
        class_summary: list[str] = []
        for sc, stats in by_class.items():
            n = stats["objectCount"]
            payload_mb = stats["payloadSize"] / 1_000_000
            class_summary.append(
                f"{sc}={n} objs/{payload_mb:.1f}MB"
            )
            if sc.upper() != "STANDARD" and n > 0:
                violations.append((bucket_name, sc, n))
        log.info(
            "F1 storage-class survey: %s -> %s",
            bucket_name,
            " · ".join(class_summary) if class_summary else "(empty)",
        )

    if violations:
        log.error(
            "F1 lockdown VIOLATION: %d (bucket, storage_class) combinations "
            "outside STANDARD. IA has no free tier on ops + 2x rate. Migrate "
            "back to STANDARD via the Cloudflare dashboard.",
            len(violations),
        )
        for bucket_name, sc, n in violations:
            log.error("  %s -> %s (%d objects)", bucket_name, sc, n)


def reconcile(
    dry_run: bool = False, delete_orphans: bool = False
) -> Dict[str, int]:
    """Single global reconciliation pass. Returns action counts.

    ``dry_run`` reports everything and writes nothing, to neither the
    bucket nor the ledger. ``delete_orphans`` is the separate opt-in
    for the one destructive action here; without it orphans are only
    reported, so the ledger-side fixes can run daily without ever
    putting user files at risk.
    """
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    counts = {
        "orphans": 0,
        "orphans_deleted": 0,
        "retained": 0,
        "phantoms": 0,
        "drifts": 0,
        "metadata_backfills": 0,
        "unbacked_videos": 0,
    }
    try:
        # Phase F lockdown via Cloudflare's authoritative storage
        # metrics — covers every R2 bucket in the account at once
        # (both aether-archive-tool and aether-archive-backups) with
        # one GraphQL call, no S3 list permission required. Runs
        # first so its log lines appear at the top of the cron
        # output; gracefully no-ops if the account-analytics token
        # is unset.
        _check_all_buckets_storage_class()

        # Existing per-object walk of the user-content bucket — needed
        # for orphan/phantom/drift detection (which IS ledger-comparative
        # and unique to the user bucket).
        r2_objects = _walk_bucket()
        log.info("walked R2 bucket: %d objects", len(r2_objects))

        # EVERY row, live or soft-deleted. A soft-deleted row still
        # identifies its object exactly, so its bytes are retained on
        # purpose and are never orphans. See the soft-delete contract
        # at the top of this module before narrowing this query.
        all_rows = db.query(StorageObject).all()
        known_keys: Set[str] = {r.r2_key for r in all_rows}

        # The live subset - the rows we are actually billing for. This
        # is the set the money-shaped checks below care about.
        active_rows = [r for r in all_rows if r.deleted_at is None]
        ledger_by_key = {r.r2_key: r for r in active_rows}
        log.info(
            "ledger rows: %d total, %d active, %d keys known",
            len(all_rows), len(active_rows), len(known_keys),
        )

        # Same fail-safe reflex as the delete brake, on the other side
        # of the diff: an empty bucket walk next to a non-empty live
        # ledger is a misconfigured endpoint or a lost list permission
        # far more often than it is total data loss, and acting on it
        # would mark every billed object a phantom in one pass. Bail.
        if not r2_objects and active_rows:
            log.error(
                "ABORT: bucket walk returned 0 objects while %d live ledger "
                "rows exist. Refusing to reconcile - check storage "
                "credentials and the bucket name before re-running.",
                len(active_rows),
            )
            db.rollback()
            return counts

        r2_keys: Set[str] = set(r2_objects.keys())
        ledger_keys: Set[str] = set(ledger_by_key.keys())

        # Orphan = the bucket has it and NOTHING in the ledger does.
        orphan_keys = r2_keys - known_keys
        # Phantom = we are billing for it and the bucket doesn't have
        # it. Only live rows qualify: a soft-deleted row with no
        # object is just a channel purge_removed already finished, or
        # a delete whose ledger write landed after the bucket DELETE.
        # That is the expected end state, not an alarm.
        phantom_keys = ledger_keys - r2_keys
        common_keys = r2_keys & ledger_keys
        # Known but not billed: soft-deleted rows sitting in their
        # grace window. We pay for these deliberately, so surface the
        # count for cost visibility and then leave them alone.
        retained_keys = (known_keys - ledger_keys) & r2_keys
        counts["retained"] = len(retained_keys)
        if retained_keys:
            retained_bytes = sum(r2_objects[k] for k in retained_keys)
            log.info(
                "retained during grace window (soft-deleted, NOT orphans): "
                "%d objects / %.1f MB",
                len(retained_keys), retained_bytes / 1_000_000,
            )

        client = r2.client()
        bucket = r2.bucket()

        # --- ORPHANS: in the bucket, unknown to the ledger entirely. ---
        counts["orphans"] = len(orphan_keys)
        allowance = _orphan_delete_allowance(len(r2_keys))
        brake_tripped = len(orphan_keys) > allowance
        if brake_tripped:
            log.error(
                "DELETE BRAKE TRIPPED: %d orphans proposed but only %d "
                "allowed this run (bucket has %d objects). Deleting NOTHING. "
                "This many orphans means something is wrong with the ledger, "
                "not with the bucket - investigate before re-running with "
                "--delete-orphans.",
                len(orphan_keys), allowance, len(r2_keys),
            )
        may_delete = (
            delete_orphans and not dry_run and not brake_tripped
            and client is not None and bucket is not None
        )
        # When the brake trips the list can be enormous, and a wall of
        # identical warnings buries the one line that matters. Show
        # enough to diagnose the pattern, then summarise.
        LOG_SAMPLE = 20
        for i, key in enumerate(sorted(orphan_keys)):
            size = r2_objects[key]
            user_id = _user_id_for_key(key) or None
            if i < LOG_SAMPLE or not brake_tripped:
                log.warning(
                    "ORPHAN (%s): %s (%d bytes, user=%s)",
                    "deleting" if may_delete else "reporting only",
                    key, size, user_id or "unknown",
                )
            elif i == LOG_SAMPLE:
                log.warning(
                    "... and %d more orphan(s) not listed",
                    len(orphan_keys) - LOG_SAMPLE,
                )
            if not may_delete:
                continue
            try:
                client.delete_object(Bucket=bucket, Key=key)
            except Exception as e:
                log.exception("orphan delete failed for %s: %s", key, e)
                continue
            db.add(
                ReconciliationLog(
                    user_id=user_id,
                    action="delete_orphan",
                    r2_key=key,
                    details_json=json.dumps({"bytes": size}),
                    ran_at=now,
                    alerted=False,
                )
            )
            counts["orphans_deleted"] += 1

        # --- PHANTOMS: live row, no object. Mark deleted + alert. ---
        for key in sorted(phantom_keys):
            row = ledger_by_key[key]
            log.error(
                "PHANTOM (data loss?): user=%s key=%s db_bytes=%d - marking deleted",
                row.user_id, key, row.bytes,
            )
            if not dry_run:
                row.deleted_at = now
                db.add(
                    ReconciliationLog(
                        user_id=row.user_id,
                        action="mark_phantom",
                        r2_key=key,
                        details_json=json.dumps({"db_bytes": row.bytes}),
                        ran_at=now,
                        alerted=True,
                    )
                )
            counts["phantoms"] += 1

        # --- DRIFT: both, bytes differ. Take R2's number + alert. ---
        for key in sorted(common_keys):
            db_bytes = ledger_by_key[key].bytes
            r2_bytes = r2_objects[key]
            if db_bytes == r2_bytes:
                continue
            log.error(
                "DRIFT: user=%s key=%s db_bytes=%d r2_bytes=%d - fixing to R2",
                ledger_by_key[key].user_id, key, db_bytes, r2_bytes,
            )
            if not dry_run:
                ledger_by_key[key].bytes = r2_bytes
                db.add(
                    ReconciliationLog(
                        user_id=ledger_by_key[key].user_id,
                        action="fix_drift",
                        r2_key=key,
                        details_json=json.dumps(
                            {"db_bytes": db_bytes, "r2_bytes": r2_bytes}
                        ),
                        ran_at=now,
                        alerted=True,
                    )
                )
            counts["drifts"] += 1

        # --- UNBACKED VIDEOS: shared Video row claims bytes_stored, but its
        # r2_key has no live storage object. The `videos` table is keyed by
        # channel, not user, so a deleted user's Video rows survive the
        # cascade with their bytes_stored + r2_key intact after the backing
        # objects are gone (auth.confirm_account_deletion nulls this at
        # deletion time now, but old orphans and any missed path land here).
        # Billing sums bytes_stored with no storage join, so an unbacked row
        # overbills for storage we do not hold - clear it. ledger_keys is the
        # live (deleted_at IS NULL) key set already built above, so this
        # reuses the exact same source of truth as PHANTOM/DRIFT.
        unbacked = (
            db.query(Video)
            .filter(
                Video.bytes_stored.isnot(None),
                Video.bytes_stored > 0,
            )
            .all()
        )
        for v in unbacked:
            if v.r2_key is not None and v.r2_key in ledger_keys:
                continue  # genuinely stored - never touch it
            log.error(
                "UNBACKED VIDEO: channel=%s youtube_id=%s bytes_stored=%d "
                "r2_key=%s - clearing (no live storage)",
                v.channel_id, v.youtube_id, v.bytes_stored, v.r2_key,
            )
            if not dry_run:
                db.add(
                    ReconciliationLog(
                        user_id=None,
                        action="video_unbacked",
                        r2_key=v.r2_key,
                        details_json=json.dumps(
                            {
                                "youtube_id": v.youtube_id,
                                "channel_id": v.channel_id,
                                "bytes_stored": v.bytes_stored,
                            }
                        ),
                        ran_at=now,
                        alerted=True,
                    )
                )
                v.bytes_stored = None
                v.r2_key = None
                v.synced_at = None
            counts["unbacked_videos"] += 1

        # --- METADATA BACKFILL: rows still on the legacy 256-byte default.
        # The default came from a flat over-estimate; new rows now record
        # the real header size from r2.metadata_bytes_for(). Backfill the
        # rest by doing one HEAD per object and computing the real value.
        # Capped per run so a big migration doesn't blow through the
        # account's daily Class B budget in one pass.
        BACKFILL_PER_RUN_CAP = 500
        legacy_rows = [
            r for r in active_rows if r.metadata_bytes == 256
        ][:BACKFILL_PER_RUN_CAP]
        if legacy_rows and client and bucket:
            for row in legacy_rows:
                try:
                    head_result = r2.head(row.r2_key, subject=row.user_id)
                except Exception:  # noqa: BLE001
                    log.exception(
                        "metadata backfill HEAD failed for %s", row.r2_key
                    )
                    continue
                if head_result is None:
                    # Phantom — handled above already, skip here.
                    continue
                real_metadata = r2.metadata_bytes_for(
                    content_type=head_result.get("ContentType"),
                    custom_meta=head_result.get("Metadata"),
                )
                if real_metadata == row.metadata_bytes:
                    continue
                log.info(
                    "metadata backfill: user=%s key=%s 256 → %d",
                    row.user_id, row.r2_key, real_metadata,
                )
                if not dry_run:
                    row.metadata_bytes = real_metadata
                    db.add(
                        ReconciliationLog(
                            user_id=row.user_id,
                            action="metadata_backfill",
                            r2_key=row.r2_key,
                            details_json=json.dumps(
                                {"old_metadata_bytes": 256,
                                 "new_metadata_bytes": real_metadata}
                            ),
                            ran_at=now,
                            alerted=False,
                        )
                    )
                counts["metadata_backfills"] += 1

        if dry_run:
            # Nothing above stages a write under --dry-run, but roll
            # back anyway so a future edit that forgets its guard
            # still can't leak a change out of a dry run.
            db.rollback()
        else:
            db.commit()

        log.info(
            "reconciliation complete (%s): orphans=%d (deleted=%d) retained=%d "
            "phantoms=%d drifts=%d metadata_backfills=%d unbacked_videos=%d",
            "dry-run" if dry_run else "applied",
            counts["orphans"], counts["orphans_deleted"], counts["retained"],
            counts["phantoms"], counts["drifts"],
            counts["metadata_backfills"], counts["unbacked_videos"],
        )
        if counts["orphans"] and not counts["orphans_deleted"]:
            log.warning(
                "%d orphan(s) reported but not deleted (%s). Their bytes are "
                "still being paid for.",
                counts["orphans"],
                "dry-run" if dry_run
                else "brake tripped" if brake_tripped
                # may_delete + nothing deleted means every delete_object
                # raised. Don't blame the flag for a bucket failure -
                # this line is the one someone reads during an incident.
                else "every delete failed, see errors above" if may_delete
                else "--delete-orphans not passed",
            )
        return counts
    except Exception:
        log.exception("reconciliation failed")
        db.rollback()
        return counts
    finally:
        db.close()


def main(argv) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Report only; don't modify the bucket or the ledger.")
    parser.add_argument(
        "--delete-orphans", action="store_true",
        help=(
            "Opt in to hard-deleting orphaned bucket objects. Off by "
            "default because it destroys user data with no undo; "
            "without it orphans are reported and left alone."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )
    reconcile(dry_run=args.dry_run, delete_orphans=args.delete_orphans)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
