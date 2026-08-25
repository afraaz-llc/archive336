"""Monthly storage + R2 ops billing run — convert accrued usage into invoices.

Run from a systemd timer at 03:00 UTC on the 3rd of every month.

For each user, compute TWO charges per period:

  1. Storage = byte-hour integral over StorageObject rows × R2's per-
     GB-hour rate × the user's markup (default 2.0×). The airtight
     method from docs/STORAGE_BILLING_DESIGN.md Phase D.

  2. Operations = sum of Class A + Class B counts from R2OperationLog
     for the period × R2's per-million rates × the same markup.
     The airtight method from the R2 ops audit (docs/CLOUDFLARE_AUDIT.md
     §14). Free tier is ignored at the user level — that's platform
     margin until exhausted, see audit doc.

Combined storage + ops total is compared to MIN_INVOICE_USD ($5). If
the combined total crosses the threshold, both line items are added
to the same Stripe invoice; below threshold, everything carries over
to next month's run.

The legacy UsageRecord-daily-snapshot sum (storage only) is still
computed for cross-check logging during the storage transition. The
ops side has no legacy method to compare against — we never billed
ops before — so the ops line is a pure addition.

Membership renewals (the $1/year fee) are NOT handled here — they're
driven by Stripe Subscriptions started at first card-add. Stripe
fires its own renewal charges on each user's anniversary and we
react via webhooks (invoice.paid / invoice.payment_failed).

Usage:
    /opt/aether/venv/bin/python -m scripts.bill        # real run
    /opt/aether/venv/bin/python -m scripts.bill --dry  # report only
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import List

from app import billing as billing_lib
from app.db import SessionLocal
from app.models import UsageRecord, User


log = logging.getLogger("aether.bill")


# How far back to look for byte-hours when a user has never been billed.
# Anything older than this is treated as a "free trial" period — we
# don't retroactively bill on the very first invoice. Keeps the first-
# ever invoice from looking like a giant retroactive charge.
_FIRST_BILL_LOOKBACK = timedelta(days=90)


def _legacy_summary(unbilled: List[UsageRecord]) -> tuple[float, float]:
    """Legacy method: GB-days sum across unbilled UsageRecord rows.

    Kept around for parallel-running cross-check during the transition
    away from the daily-snapshot meter. See storage billing design doc.
    """
    gb_days = sum(billing_lib.bytes_to_gb(r.bytes_stored) for r in unbilled)
    usd = billing_lib.gb_days_to_usd(gb_days)
    return gb_days, usd


def main(argv: list[str]) -> int:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )
    dry_run = "--dry" in argv
    today = datetime.now(timezone.utc)

    invoiced = 0
    carry_over = 0
    skipped_no_card = 0
    nothing_unbilled = 0
    total_charged_usd = 0.0

    db = SessionLocal()
    try:
        users = db.query(User).all()
        log.info(
            "storage billing run starting (dry=%s, method=ledger) over %d users",
            dry_run,
            len(users),
        )

        for user in users:
            # ---- Storage (byte-hour integral, the only billed line) ----
            # R2 Class A/B ops used to be billed here too; they're now
            # platform-absorbed (see Expenses tab Per-account bucket)
            # since the per-user math wasn't worth the invoice complexity
            # at our scale.
            period_start = user.last_billed_at or (today - _FIRST_BILL_LOOKBACK)
            if period_start.tzinfo is None:
                period_start = period_start.replace(tzinfo=timezone.utc)
            # Shared-pool v2 is the authoritative source now. v1 is
            # logged side-by-side for one more billing cycle as a
            # safety net; the reconcile cron also still cross-checks.
            byte_hours = billing_lib.compute_user_byte_hours_v2(
                db, user.id, period_start, today
            )
            byte_hours_v1 = billing_lib.compute_user_byte_hours(
                db, user.id, period_start, today
            )
            markup = billing_lib.get_user_storage_markup(user)
            storage_usd = billing_lib.byte_hours_to_user_charge_usd(
                byte_hours, markup
            )
            storage_r2_cost = billing_lib.byte_hours_to_storage_cost_usd(byte_hours)
            total_usd = storage_usd
            v1_v2_delta_pct = (
                ((byte_hours - byte_hours_v1) / byte_hours_v1 * 100.0)
                if byte_hours_v1
                else 0.0
            )
            log.info(
                "user %s: v2_bh=%.0f v1_bh=%.0f v2-v1=%.2f%%",
                user.id,
                byte_hours,
                byte_hours_v1,
                v1_v2_delta_pct,
            )

            # ---- Legacy storage method — cross-check log only ----
            unbilled = (
                db.query(UsageRecord)
                .filter(
                    UsageRecord.user_id == user.id,
                    UsageRecord.billed.is_(False),
                )
                .order_by(UsageRecord.day)
                .all()
            )
            legacy_gb_days, legacy_usd = (
                _legacy_summary(unbilled) if unbilled else (0.0, 0.0)
            )
            if storage_usd > 0 or legacy_usd > 0:
                delta = storage_usd - legacy_usd
                delta_pct = (delta / legacy_usd * 100.0) if legacy_usd else 0.0
                log.info(
                    "user %s: storage ledger=$%.4f (bh=%.0f r2=$%.4f markup=%.2fx) "
                    "legacy=$%.4f (gb_days=%.2f rows=%d) delta=$%+.4f (%+.1f%%)",
                    user.id,
                    storage_usd,
                    byte_hours,
                    storage_r2_cost,
                    markup,
                    legacy_usd,
                    legacy_gb_days,
                    len(unbilled),
                    delta,
                    delta_pct,
                )

            # Decide whether this user gets billed today.
            if not billing_lib.should_bill_now(total_usd, today):
                if total_usd > 0 or unbilled:
                    carry_over += 1
                    log.info(
                        "user %s: carry over storage $%.4f to next month (threshold $%.2f)",
                        user.id,
                        storage_usd,
                        billing_lib.MIN_INVOICE_USD,
                    )
                else:
                    nothing_unbilled += 1
                continue

            # Need a Stripe customer to invoice. If we don't have one
            # yet, the user never finished card setup; skip and the
            # 402 gate already prevents new work for them.
            if not user.stripe_customer_id:
                skipped_no_card += 1
                log.warning(
                    "user %s qualifies for billing but has no stripe_customer_id",
                    user.id,
                )
                continue

            storage_cents = max(0, round(storage_usd * 100))
            if storage_cents <= 0:
                # Defensive: should_bill_now returning True implies
                # storage >= threshold, but belt-and-braces.
                continue

            period_end = today
            storage_desc = billing_lib.storage_period_description(
                period_start, period_end
            )
            log.info(
                "user %s: storage line bh=%.0f usd=%.4f cents=%d period=%s..%s",
                user.id,
                byte_hours,
                storage_usd,
                storage_cents,
                period_start.date(),
                period_end.date(),
            )

            if dry_run:
                continue

            # ---- Stage the storage line ----
            try:
                billing_lib.add_invoice_item(
                    customer_id=user.stripe_customer_id,
                    amount_cents=storage_cents,
                    description=storage_desc,
                    period_start=period_start,
                    period_end=period_end,
                )
            except Exception:
                log.exception(
                    "Stripe storage line failed for user %s", user.id
                )
                continue

            try:
                result = billing_lib.finalize_invoice(user.stripe_customer_id)
            except Exception:
                log.exception(
                    "Stripe invoice finalize failed for user %s", user.id
                )
                continue

            log.info(
                "user %s: invoice %s status=%s amount_cents=%d",
                user.id,
                result.get("invoice_id"),
                result.get("status"),
                result.get("amount_cents"),
            )

            # Stamp the user's last_billed_at so the next run computes
            # byte-hours AND ops counts from period_end forward instead
            # of the old _FIRST_BILL_LOOKBACK window. Also retire any
            # outstanding UsageRecord rows for this period so they
            # don't carry over into legacy storage comparisons going
            # forward — the ledger has already covered them. Webhook
            # flow on Stripe failure also re-stamps last_billed_at, but
            # we set it here too for the happy path so the next cron
            # has the right start.
            #
            # The ops ledger (R2OperationLog) is NOT mutated here —
            # the period query uses last_billed_at as the start cutoff,
            # so already-billed days are naturally excluded next run.
            # Keeping ops rows intact is also useful for the reconciliation
            # cron and for historical "show me my ops usage" queries.
            for r in unbilled:
                r.billed = True
            user.last_billed_at = today
            db.commit()

            invoiced += 1
            total_charged_usd += total_usd

        log.info(
            "storage billing run done: invoiced=%d carry_over=%d "
            "no_card=%d nothing=%d total=$%.2f",
            invoiced,
            carry_over,
            skipped_no_card,
            nothing_unbilled,
            total_charged_usd,
        )
        return 0
    except Exception:
        log.exception("billing run failed")
        db.rollback()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
