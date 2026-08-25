"""Daily Stripe-state reconciliation — catch drift between our DB and Stripe.

Webhook events are how we *normally* keep ``User.payment_status`` in
sync with Stripe. But webhooks can be missed:
  - Backend was down when Stripe sent the event (retry window may
    not cover the outage)
  - Webhook signature failure dropped the event silently
  - Manual dashboard action (admin pause / unpause / cancel) that
    didn't surface a ``customer.subscription.updated`` event we
    listened for at the time
  - The handler crashed before commit

This cron does a daily sweep: for every user we have a
``stripe_customer_id`` for, ask Stripe what subscriptions they have
and compare against ``User.payment_status``. Any drift is logged + an
audit row is written + (unless ``--dry-run``) the local row is fixed.

Stripe is the source of money so Stripe wins every disagreement —
same philosophy as the R2 storage reconciliation script.

Usage:
    /opt/aether/venv/bin/python -m scripts.reconcile_stripe
    /opt/aether/venv/bin/python -m scripts.reconcile_stripe --dry-run

Rate: 1 API call per active user; at single-digit users this is
trivially cheap. Bulk reconciliation can come if/when we cross 1k+
users.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Optional

import stripe

from app.db import SessionLocal
from app.models import StripeAuditLog, User


log = logging.getLogger("aether.reconcile_stripe")


def _configure_stripe() -> bool:
    """Set the Stripe API key from env. Returns False if unset."""
    key = os.environ.get("STRIPE_SECRET_KEY")
    if not key:
        log.warning("STRIPE_SECRET_KEY unset; nothing to reconcile")
        return False
    stripe.api_key = key
    return True


def _desired_status_from_stripe(customer_id: str) -> Optional[str]:
    """Ask Stripe what payment_status this customer SHOULD be in.

    Returns:
        'active'    - at least one active or trialing subscription
        'past_due'  - subscriptions exist but all are past_due/unpaid
        'canceled'  - subscriptions exist but all are canceled/expired
        'none'      - no subscriptions at all (post-cleanup or never started)
        None        - couldn't decide (API error)
    """
    try:
        subs = stripe.Subscription.list(customer=customer_id, status="all", limit=100)
    except Exception as e:  # noqa: BLE001
        log.exception("Stripe.Subscription.list failed for %s: %s", customer_id, e)
        return None

    if not subs.data:
        return "none"

    statuses = {s.status for s in subs.data}

    # Live subscriptions win - if any are running, the user is active.
    if "active" in statuses or "trialing" in statuses:
        return "active"
    # If every sub is in a payment-failure state, surface past_due so
    # dunning + UI nudges fire.
    if statuses and statuses.issubset({"past_due", "unpaid", "incomplete"}):
        return "past_due"
    # Otherwise everything is dead (canceled, incomplete_expired) -
    # the user has no active membership.
    return "canceled"


def reconcile(dry_run: bool = False) -> dict:
    """Single global Stripe-state reconciliation pass."""
    counts = {"checked": 0, "in_sync": 0, "drift_fixed": 0, "drift_dry_run": 0, "errors": 0}

    if not _configure_stripe():
        return counts

    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        users = (
            db.query(User)
            .filter(User.stripe_customer_id.isnot(None))
            .all()
        )
        log.info("checking %d users with Stripe customers", len(users))

        for user in users:
            counts["checked"] += 1
            desired = _desired_status_from_stripe(user.stripe_customer_id)
            if desired is None:
                counts["errors"] += 1
                continue

            current = user.payment_status or "none"
            if desired == current:
                counts["in_sync"] += 1
                continue

            # Drift detected. Log loudly + record audit row + (unless
            # dry-run) flip the local status to match Stripe.
            log.warning(
                "STRIPE DRIFT: user=%s (cus=%s) db=%s stripe_says=%s%s",
                user.id,
                user.stripe_customer_id,
                current,
                desired,
                " (dry-run — not changing)" if dry_run else " — fixing",
            )

            if dry_run:
                counts["drift_dry_run"] += 1
                continue

            # Use a synthetic stripe_event_id so the unique constraint
            # still applies (one drift-fix per user per second is
            # plenty granular; in practice these happen once per day
            # at most per user).
            audit = StripeAuditLog(
                stripe_event_id=f"reconcile-{user.id}-{int(now.timestamp())}",
                event_type="reconcile.subscription_drift",
                stripe_customer_id=user.stripe_customer_id,
                user_id=user.id,
                payload_json=json.dumps(
                    {
                        "db_payment_status": current,
                        "stripe_desired_status": desired,
                        "fixed_at": now.isoformat(),
                    }
                ),
                handled=True,
                notes=f"drift fix: {current} -> {desired}",
            )
            db.add(audit)
            user.payment_status = desired
            counts["drift_fixed"] += 1

        if not dry_run:
            db.commit()

        log.info(
            "stripe reconciliation complete (%s): "
            "checked=%d in_sync=%d drift_fixed=%d drift_dry_run=%d errors=%d",
            "dry-run" if dry_run else "applied",
            counts["checked"],
            counts["in_sync"],
            counts["drift_fixed"],
            counts["drift_dry_run"],
            counts["errors"],
        )
        return counts
    except Exception:
        log.exception("stripe reconciliation failed")
        db.rollback()
        return counts
    finally:
        db.close()


def main(argv) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report only; don't modify the local payment_status.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )
    reconcile(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
