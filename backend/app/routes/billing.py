"""Billing endpoints — Stripe customer setup + status + webhooks.

Public endpoints (require auth but not payment_status='active'):
- POST /api/billing/setup-intent  : create Stripe customer (if needed),
                                    return SetupIntent client_secret for
                                    the frontend to feed into Stripe Elements
- GET  /api/billing/portal        : returns a hosted Customer Portal URL
                                    where the user manages their card
- GET  /api/billing/status        : current payment_status, accrued usage,
                                    balance toward next bill, etc.

Webhook (no auth — verified by HMAC signature):
- POST /api/billing/webhook       : Stripe → us, payment events that flip
                                    User.payment_status

Guards live in app.security as dependencies; this file just wires the
endpoints.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import billing
from app.db import get_db
from app.models import StripeAuditLog, UsageRecord, User, UserChannel, UserChannelVideo
from app.security import get_current_user


log = logging.getLogger("archive336.billing")

router = APIRouter()


# ---------- Pricing ----------


@router.get("/prices")
def get_prices() -> Dict[str, Any]:
    """Single source of truth for what we charge users.

    Public (no auth) - the frontend uses this to populate cost
    estimates and re-archive previews. Backend bills off the same
    constants from app.billing, so the page can never claim a price
    that differs from the actual invoice.

    Returns:
        storagePerGbMonth: USD per GB-month of stored archive.
        downloadPerGb:     USD per GB downloaded by the user.
    """
    return {
        "storagePerGbMonth": billing.PRICE_PER_GB_PER_MONTH_USD,
        "downloadPerGb": billing.DOWNLOAD_PRICE_PER_GB_USD,
    }


# ---------- Customer setup ----------


@router.post("/setup-intent")
def create_setup_intent(
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Begin the card-on-file flow.

    Idempotent: re-running for the same user returns a fresh SetupIntent
    against the same Stripe Customer. The frontend uses the returned
    client_secret with Stripe Elements to collect a card without it
    ever touching our servers.
    """
    customer_id = current.stripe_customer_id
    if not customer_id:
        try:
            customer_id = billing.get_or_create_customer(
                email=current.email, username=current.username
            )
        except Exception:
            log.exception("Stripe customer create failed for user %s", current.id)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Couldn't reach the payment processor.",
            )
        current.stripe_customer_id = customer_id
        db.commit()

    try:
        return billing.create_setup_intent(customer_id)
    except Exception:
        log.exception("Stripe setup intent failed for user %s", current.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Couldn't start the card setup flow.",
        )


@router.post("/setup-confirm")
def confirm_setup(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Verify the card is attached and start the user's $1/yr membership.

    Flow on the happy path:
      1. Frontend calls this after Stripe Elements' confirmSetup() succeeds
      2. We confirm the customer has a payment method attached
      3. We create a Stripe Subscription against the membership Price —
         Stripe immediately invoices $1 and charges the new card
      4. invoice.paid webhook fires, flips payment_status to 'active'
      5. We also flip status here synchronously so the UI doesn't
         have to wait on the webhook for the same-page-load reload

    Idempotent: if the user already has an active membership we skip
    the subscription create (Stripe would just return the existing one
    via our get_active_membership_subscription guard, but we short-
    circuit earlier).
    """
    if current.payment_status == "active":
        return {"paymentStatus": "active"}
    if not current.stripe_customer_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Add a payment method first.",
        )
    try:
        has_card = billing.has_any_payment_method(current.stripe_customer_id)
    except Exception:
        log.exception("Stripe payment-method check failed for user %s", current.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Couldn't reach the payment processor.",
        )
    if not has_card:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No payment method on file yet.",
        )

    # Start the membership subscription. This immediately charges $1
    # against the just-attached card. If the charge fails (declined,
    # insufficient funds, etc.), Stripe raises and we surface it to
    # the user — payment_status stays 'none' and they can retry with
    # a different card via the same dialog.
    try:
        sub = billing.create_membership_subscription(current.stripe_customer_id)
    except stripe.error.CardError as e:
        log.warning(
            "membership subscription card declined for user %s: %s",
            current.id,
            e.user_message or e,
        )
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=e.user_message or "Card was declined. Try a different card.",
        )
    except Exception:
        log.exception(
            "membership subscription create failed for user %s", current.id
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Couldn't start the membership. Please try again.",
        )

    log.info(
        "user %s membership subscription %s status=%s (created=%s)",
        current.id,
        sub.get("subscription_id"),
        sub.get("status"),
        sub.get("created"),
    )

    current.payment_status = "active"
    db.commit()
    return {"paymentStatus": "active", "subscription": sub}


class SetPlanRequest(BaseModel):
    tier: str


def _restore_from_cancel(db: Session, current: User) -> None:
    """After a canceled user picks a plan again: restore the channels the
    cancel moved into grace (removed_at >= plan_canceled_at, still inside
    the window) and back-charge the storage we held for them during the
    grace window — deferred billing, not free storage, so cancel→resubscribe
    can't be used to dodge storage fees. Channels the user had individually
    disconnected earlier (older removed_at) are left as-is. Clears
    plan_canceled_at. Does NOT commit — the caller commits.
    """
    from sqlalchemy import func  # noqa: WPS433

    from app import archive as archive_lib  # noqa: WPS433
    from app import storage_ledger  # noqa: WPS433
    from app.models import Channel, Video  # noqa: WPS433

    canceled_at = current.plan_canceled_at
    if canceled_at is None:
        return
    if canceled_at.tzinfo is None:
        canceled_at = canceled_at.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)

    graced = (
        db.query(UserChannel)
        .filter(
            UserChannel.user_id == current.id,
            UserChannel.removed_at.is_not(None),
            UserChannel.removed_at >= canceled_at,
        )
        .all()
    )

    total_byte_hours = 0.0
    for uc in graced:
        removed_at = uc.removed_at
        if removed_at.tzinfo is None:
            removed_at = removed_at.replace(tzinfo=timezone.utc)
        grace_hours = max(0.0, (now - removed_at).total_seconds() / 3600.0)

        channel = (
            db.query(Channel)
            .filter(Channel.youtube_id == uc.channel_id)
            .one_or_none()
        )
        if channel is not None:
            stored_bytes = (
                db.query(func.coalesce(func.sum(Video.bytes_stored), 0))
                .filter(
                    Video.channel_id == channel.id,
                    Video.bytes_stored.is_not(None),
                    Video.bytes_stored > 0,
                )
                .scalar()
                or 0
            )
            total_byte_hours += float(stored_bytes) * grace_hours
            archive_lib.ensure_subscription(db, current.id, channel.id)
        uc.removed_at = None
        storage_ledger.propagate_channel_restore(db, current.id, uc.channel_id)

    # Stage the deferred grace-period storage onto the next Stripe invoice
    # (mirrors the real byte-hour user-charge path). Skip if it rounds to $0.
    if total_byte_hours > 0 and current.stripe_customer_id:
        # Use the user's effective markup (tier + any override) so the
        # back-charge rate matches exactly what the real bill would charge.
        back_usd = billing.byte_hours_to_user_charge_usd(
            total_byte_hours, markup=billing.get_user_storage_markup(current)
        )
        cents = int(round(back_usd * 100))
        if cents >= 1:
            try:
                billing.add_invoice_item(
                    current.stripe_customer_id,
                    cents,
                    "Storage held during your cancellation grace period",
                )
            except Exception:
                log.exception(
                    "grace back-charge invoice item failed for user %s",
                    current.id,
                )

    log.info(
        "user %s resubscribed: restored %d graced channel(s), back-charge byte_hours=%.0f",
        current.id,
        len(graced),
        total_byte_hours,
    )
    current.plan_canceled_at = None


@router.post("/plan")
def set_plan(
    payload: SetPlanRequest,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Subscribe to / switch to a plan tier (e.g. basic <-> creator).

    Requires a card on file. Cancels the user's current plan
    subscription (if any) and starts the chosen tier's, then flips
    User.tier so storage bills at the new tier's rate. Immediate, no
    proration - a clean cancel + re-subscribe.
    """
    tier = (payload.tier or "").strip().lower()
    if tier not in billing.PLAN_PRICE_ENV:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown plan."
        )
    # Capture cancel state before we flip to active — drives the restore +
    # grace-period back-charge at the end.
    was_canceled = (
        current.payment_status == "canceled"
        and current.plan_canceled_at is not None
    )
    if not current.stripe_customer_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Add a payment method first.",
        )
    try:
        has_card = billing.has_any_payment_method(current.stripe_customer_id)
    except Exception:
        log.exception("payment-method check failed for user %s", current.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Couldn't reach the payment processor.",
        )
    if not has_card:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No payment method on file yet.",
        )
    try:
        sub = billing.set_plan_subscription(current.stripe_customer_id, tier)
    except stripe.error.CardError as e:
        log.warning(
            "plan switch card declined for user %s: %s",
            current.id,
            e.user_message or e,
        )
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=e.user_message or "Card was declined. Try a different card.",
        )
    except Exception:
        log.exception("plan switch to %s failed for user %s", tier, current.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Couldn't change your plan. Please try again.",
        )

    current.tier = tier
    current.payment_status = "active"
    if was_canceled:
        # Coming back from a cancel: restore the graced channels + settle
        # the deferred storage held during the grace window.
        _restore_from_cancel(db, current)
    db.commit()
    log.info(
        "user %s set plan=%s (sub=%s, created=%s)",
        current.id,
        tier,
        sub.get("subscription_id"),
        sub.get("created"),
    )
    return {"paymentStatus": "active", "tier": tier, "subscription": sub}


@router.post("/cancel")
def cancel_plan(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Cancel the user's plan.

    Stops the recurring Stripe membership charge, moves every active channel
    into the same 30-day removed-grace window a disconnect uses (data kept,
    metering stops), and marks the account canceled with plan_canceled_at as
    the grace clock. Resubscribing (POST /plan) before the purge restores
    everything AND back-charges the storage held during grace; if they never
    come back, the existing daily purge removes it after 30 days.
    """
    from app import archive as archive_lib  # noqa: WPS433
    from app import storage_ledger  # noqa: WPS433
    from app.models import Channel, UserChannelSubscription  # noqa: WPS433

    now = datetime.now(timezone.utc)

    # 1. Stop the recurring membership charge.
    if current.stripe_customer_id:
        try:
            billing.cancel_active_plan_subscription(current.stripe_customer_id)
        except Exception:
            log.exception("cancel: stripe sub cancel failed for user %s", current.id)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Couldn't reach the payment processor. Please try again.",
            )

    # 2. Move every active channel into the grace window (stops the meter +
    #    starts the same 30-day restore/purge clock as a disconnect).
    rows = (
        db.query(UserChannelSubscription, Channel)
        .join(Channel, Channel.id == UserChannelSubscription.channel_id)
        .filter(
            UserChannelSubscription.user_id == current.id,
            UserChannelSubscription.unsubscribed_at.is_(None),
        )
        .all()
    )
    for _sub, channel in rows:
        archive_lib.soft_delete_subscription(db, current.id, channel.id)
        legacy = db.get(UserChannel, (current.id, channel.youtube_id))
        if legacy is not None and legacy.removed_at is None:
            legacy.removed_at = now
            storage_ledger.propagate_channel_soft_delete(
                db, current.id, channel.youtube_id, removed_at=now
            )

    # 3. Mark canceled + stamp the grace clock.
    current.payment_status = "canceled"
    current.plan_canceled_at = now
    db.commit()
    log.info(
        "user %s canceled plan (%d channel(s) -> grace)", current.id, len(rows)
    )
    return {"paymentStatus": "canceled", "channelsGraced": len(rows)}


@router.get("/portal")
def billing_portal(
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, str]:
    """Get a one-time URL into Stripe's hosted Customer Portal."""
    if not current.stripe_customer_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Add a payment method first.",
        )
    # Cloudflare-aware origin: prefer the Origin header, fall back to env
    origin = request.headers.get("origin") or os.environ.get(
        "ARCHIVE336_FRONTEND_ORIGIN", "https://archive336.com"
    )
    # Stripe's portal uses this single URL for BOTH the "Return to
    # ARCHIVE336" link AND the header logo, so they can't go to different
    # pages. Send both to the Billing tab — where the user launched the
    # portal from — rather than the default (General) tab.
    return_url = f"{origin.rstrip('/')}/settings?tab=billing"
    try:
        url = billing.create_billing_portal_session(
            current.stripe_customer_id, return_url
        )
    except Exception:
        log.exception("Stripe portal session failed for user %s", current.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Couldn't reach the payment processor.",
        )
    return {"url": url}


@router.get("/status")
def billing_status(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Snapshot for the Settings → Plan card.

    Returns the user's payment status + a live estimate of what they
    currently owe based on storage today. The actual bill is generated
    by the monthly cron, this is just a "heads up".

    Stripe webhook (payment_method.attached / detached, setup_intent
    .succeeded, invoice.paid / failed) keeps payment_status in sync,
    so /status just reads the cached value — no per-request Stripe
    round-trip needed.
    """
    # Current storage = sum of fileSizeBytes across the user's videos.
    # Excludes videos under soft-deleted (removed_at) channels - the
    # user has asked us to drop those, and the meter is already
    # ignoring them so the displayed number should match.
    active_channel_ids = {
        cid
        for (cid,) in (
            db.query(UserChannel.channel_id)
            .filter(
                UserChannel.user_id == current.id,
                UserChannel.removed_at.is_(None),
            )
            .all()
        )
    }
    current_bytes = 0
    rows = (
        db.query(UserChannelVideo)
        .filter(
            UserChannelVideo.user_id == current.id,
            UserChannelVideo.channel_id.in_(active_channel_ids),
        )
        .all()
        if active_channel_ids
        else []
    )
    for r in rows:
        try:
            import json as _json
            data = _json.loads(r.data_json)
            n = data.get("fileSizeBytes")
            if isinstance(n, int):
                current_bytes += n
        except Exception:
            continue

    # Unbilled accrued cost = sum of unbilled UsageRecord rows × price
    unbilled_records = (
        db.query(UsageRecord)
        .filter(UsageRecord.user_id == current.id, UsageRecord.billed.is_(False))
        .all()
    )
    gb_days = sum(billing.bytes_to_gb(r.bytes_stored) for r in unbilled_records)
    unbilled_usd = round(billing.gb_days_to_usd(gb_days), 4)

    # If we billed today, what would they pay this month? Naive estimate:
    # current GB × 30 days × price/GB-mo / 30 = current GB × price/GB-mo.
    # Storage price follows the user's effective tier (Basic $0.02,
    # Creator $0.01, ...).
    tier = current.effective_tier
    tier_storage_price = billing.STORAGE_PRICE_PER_GB_MONTH_BY_TIER.get(
        tier, billing.DEFAULT_STORAGE_PRICE_PER_GB_MONTH
    )
    current_gb = billing.bytes_to_gb(current_bytes)
    monthly_estimate_usd = round(current_gb * tier_storage_price, 2)

    # Whether a card is on file + which card - decoupled from
    # payment_status, since a user can save a card without being on a
    # plan. Read live from Stripe.
    has_payment_method = False
    payment_method = None
    if current.stripe_customer_id:
        try:
            payment_method = billing.get_default_payment_method(
                current.stripe_customer_id
            )
            if payment_method is not None:
                has_payment_method = True
            else:
                has_payment_method = billing.has_any_payment_method(
                    current.stripe_customer_id
                )
        except Exception:
            log.exception(
                "payment method check failed for user %s", current.id
            )

    return {
        "paymentStatus": current.payment_status,
        "hasPaymentMethod": has_payment_method,
        "paymentMethod": payment_method,
        "stripeCustomerId": current.stripe_customer_id,
        "currentBytes": current_bytes,
        "currentGb": round(current_gb, 3),
        "unbilledUsd": unbilled_usd,
        "monthlyEstimateUsd": monthly_estimate_usd,
        "tier": tier,
        "billThresholdUsd": billing.MIN_INVOICE_USD,
        "pricePerGbPerMonthUsd": tier_storage_price,
        "annualFeeUsd": billing.ANNUAL_FEE_USD,
        "billingDayOfMonth": billing.BILLING_DAY_OF_MONTH,
        "lastBilledAt": (
            current.last_billed_at.isoformat() if current.last_billed_at else None
        ),
    }


@router.get("/invoices")
def get_invoices(
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Recent Stripe invoices for the billing-history section. Returns an
    empty list if the user has no Stripe customer yet or the lookup fails."""
    if not current.stripe_customer_id:
        return {"invoices": []}
    try:
        invoices = billing.list_invoices(current.stripe_customer_id)
    except Exception:
        log.exception("list_invoices failed for user %s", current.id)
        invoices = []
    return {"invoices": invoices}


# ---------- Stripe → us webhook ----------


@router.post("/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)) -> Response:
    """Receive payment events from Stripe.

    Verifies HMAC signature using STRIPE_WEBHOOK_SECRET.

    Customer-scoped events (toggle user.payment_status):
      - invoice.paid                → 'active', set last_billed_at
      - invoice.payment_failed      → 'past_due'
      - customer.subscription.deleted, customer.deleted → 'canceled'
      - setup_intent.succeeded, payment_method.attached → 'active'
      - payment_method.detached     → 'canceled' if last card
      - invoice.upcoming            → log; eventually 7-day pre-charge email

    System events (no customer_id, log + alert only):
      - payout.paid                 → log
      - payout.failed               → ALERT (bank rejected our payout)
      - radar.early_fraud_warning.created/.updated → ALERT
                                      (Stripe flagged a charge as likely
                                      fraud before the cardholder disputed;
                                      refund window is short)
      - charge.dispute.funds_withdrawn → ALERT (Stripe pulled money from
                                      our balance for a chargeback)
      - charge.dispute.funds_reinstated → log (dispute won, money back)
      - refund.created              → log + audit (no DB mutation per the
                                      exceptions-only refund policy; see
                                      docs/STRIPE_AUDIT.md §4 + §10.5)
      - refund.failed               → ALERT (refund couldn't be processed,
                                      typically closed card account)
    """
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")

    if secret:
        try:
            event = stripe.Webhook.construct_event(payload, sig, secret)
        except (ValueError, stripe.error.SignatureVerificationError):
            log.warning("Stripe webhook signature failed")
            raise HTTPException(status_code=400, detail="bad signature")
    else:
        # Webhook secret not yet configured — accept but log loudly.
        # Once the user adds STRIPE_WEBHOOK_SECRET to .env this branch
        # goes away. Until then we trust the request, which is fine for
        # test-mode but MUST be locked down before going live.
        log.warning(
            "STRIPE_WEBHOOK_SECRET unset — accepting webhook without verification"
        )
        try:
            import json as _json
            event = _json.loads(payload)
        except Exception:
            raise HTTPException(status_code=400, detail="bad payload")

    # stripe.Webhook.construct_event returns a stripe.Event which is
    # dict-like in some SDK versions and pydantic-style attribute-access
    # only in others (varies by API version too). The isinstance(dict)
    # check we used to use silently returned None for non-dict-subclass
    # objects, dropping the customer_id and skipping every webhook with
    # an "unknown customer None" warning. Helper below tries dict access
    # first, falls back to attribute access, never raises.
    def _field(o: Any, key: str) -> Any:
        if o is None:
            return None
        try:
            v = o[key]
            if v is not None:
                return v
        except (KeyError, TypeError):
            pass
        return getattr(o, key, None)

    event_type = _field(event, "type")
    data = _field(event, "data")
    obj = _field(data, "object")
    stripe_event_id = _field(event, "id") or ""
    log.info("stripe webhook: %s id=%s", event_type, stripe_event_id)

    # ---------- Replay guard ----------
    # Stripe redelivers events if we're slow to 200 or if our previous
    # 200 was lost. The audit log's unique stripe_event_id makes this
    # easy: same event_id = already processed = no-op.
    if stripe_event_id:
        replay = (
            db.query(StripeAuditLog)
            .filter(StripeAuditLog.stripe_event_id == stripe_event_id)
            .first()
        )
        if replay is not None:
            log.info(
                "stripe webhook replay (already processed at %s): %s id=%s",
                replay.received_at, event_type, stripe_event_id,
            )
            return Response(status_code=200)

    # ---------- Audit row ----------
    # Insert eagerly so EVERY event Stripe sends us is on record, even
    # if our handler crashes mid-processing. We mutate handled/notes/
    # user_id/customer_id on the way through and commit once at the end
    # of each branch.
    import json as _json
    try:
        payload_dump = _json.dumps(
            event.to_dict() if hasattr(event, "to_dict") else event,
            default=str,
        )
    except Exception:
        payload_dump = None
    audit = StripeAuditLog(
        stripe_event_id=stripe_event_id or f"local-{datetime.now(timezone.utc).isoformat()}",
        event_type=event_type or "unknown",
        payload_json=payload_dump,
        handled=False,
    )
    db.add(audit)

    def _finish(handled: bool, notes: Optional[str] = None) -> Response:
        """Commit the audit row and return 200.

        Used by every branch (system events, customer events,
        unknown-customer fallback) so the audit log captures the
        outcome regardless of which path we took.
        """
        audit.handled = handled
        if notes is not None:
            audit.notes = notes
        try:
            db.commit()
        except Exception:
            log.exception("stripe audit commit failed for %s", event_type)
            db.rollback()
        return Response(status_code=200)

    # ---------- System events (no associated user) ----------
    # These don't belong to a specific customer, so handle them before
    # the user lookup. Logged + audited; no DB user mutation.

    if event_type == "payout.paid":
        amt, cur = _field(obj, "amount"), _field(obj, "currency")
        pid = _field(obj, "id")
        log.info(
            "STRIPE PAYOUT PAID: %s %s arrival=%s id=%s",
            amt, cur, _field(obj, "arrival_date"), pid,
        )
        return _finish(handled=True, notes=f"payout {pid} paid {amt} {cur}")

    if event_type == "payout.failed":
        # Loud — every payout failure means money is stuck at Stripe and
        # the bank account is misconfigured or unreachable. Needs a human.
        amt = _field(obj, "amount")
        cur = _field(obj, "currency")
        code = _field(obj, "failure_code")
        msg = _field(obj, "failure_message")
        pid = _field(obj, "id")
        log.error(
            "STRIPE PAYOUT FAILED: %s %s code=%s msg=%s id=%s "
            "— investigate bank account / Stripe balance immediately",
            amt, cur, code, msg, pid,
        )
        return _finish(
            handled=True,
            notes=f"payout {pid} FAILED ({code}: {msg})",
        )

    if event_type in (
        "radar.early_fraud_warning.created",
        "radar.early_fraud_warning.updated",
    ):
        # Stripe Radar saw something that looks like a fraudulent charge
        # BEFORE the cardholder formally disputes. If `actionable` is
        # true we have a window to refund and skip the $15 dispute fee.
        # For MVP: alert + log only - manual review in dashboard. Auto-
        # refund based on fraud_type could come later as policy.
        suffix = event_type.rsplit(".", 1)[-1]
        charge = _field(obj, "charge")
        ftype = _field(obj, "fraud_type")
        actionable = _field(obj, "actionable")
        log.error(
            "STRIPE EARLY FRAUD WARNING (%s): charge=%s fraud_type=%s "
            "actionable=%s id=%s — review in dashboard, refund within "
            "the dispute-prevention window to avoid the $15 fee",
            suffix, charge, ftype, actionable, _field(obj, "id"),
        )
        return _finish(
            handled=True,
            notes=f"EFW {suffix} charge={charge} type={ftype} actionable={actionable}",
        )

    if event_type == "charge.dispute.funds_withdrawn":
        # Chargeback: Stripe pulled money from our balance to cover the
        # disputed amount + $15 fee. Won't come back unless we submit
        # evidence and win.
        charge = _field(obj, "charge")
        amt, cur = _field(obj, "amount"), _field(obj, "currency")
        reason = _field(obj, "reason")
        log.error(
            "STRIPE DISPUTE FUNDS WITHDRAWN: charge=%s amount=%s %s "
            "reason=%s id=%s — submit evidence in dashboard if disputing",
            charge, amt, cur, reason, _field(obj, "id"),
        )
        return _finish(
            handled=True,
            notes=f"dispute withdrawn charge={charge} {amt} {cur} ({reason})",
        )

    if event_type == "charge.dispute.funds_reinstated":
        charge = _field(obj, "charge")
        amt, cur = _field(obj, "amount"), _field(obj, "currency")
        log.info(
            "STRIPE DISPUTE FUNDS REINSTATED: charge=%s amount=%s %s id=%s",
            charge, amt, cur, _field(obj, "id"),
        )
        return _finish(
            handled=True,
            notes=f"dispute reinstated charge={charge} {amt} {cur}",
        )

    if event_type == "refund.created":
        # Refund records — fired whenever a refund is created, whether by
        # us (manual dashboard refund, or future auto-refund-on-EFW logic)
        # or by Stripe itself (rare; only happens for compliance/legal
        # reasons we'd want to know about anyway).
        #
        # Per the refund policy in STRIPE_AUDIT.md §4: refunds in our
        # model are exception-handling (data loss, billing error, fraud
        # prevention), not a general customer-service primitive. The
        # handler logs + audits but does NOT mutate any DB state:
        #
        #   - We don't unmark related UsageRecord rows. If we refunded a
        #     storage invoice, the user got the storage for free for that
        #     period. Re-billing them next cycle would be a worse outcome
        #     than leaving billed=True.
        #   - We don't change User.payment_status. The user can still
        #     have an active subscription after a one-off refund.
        charge = _field(obj, "charge")
        amt, cur = _field(obj, "amount"), _field(obj, "currency")
        reason = _field(obj, "reason") or "(no reason)"
        refund_id = _field(obj, "id")
        log.info(
            "STRIPE REFUND CREATED: %s %s charge=%s reason=%s refund=%s",
            amt, cur, charge, reason, refund_id,
        )
        return _finish(
            handled=True,
            notes=f"refund created {amt} {cur} charge={charge} reason={reason}",
        )

    if event_type == "refund.failed":
        # Rare but real. The refund couldn't be processed - typically
        # because the original card account is closed and the refund
        # has nowhere to land. Stripe will sometimes auto-fall-back to
        # ACH but if that fails too we have to contact the customer.
        # Loud alert.
        charge = _field(obj, "charge")
        amt, cur = _field(obj, "amount"), _field(obj, "currency")
        failure_reason = _field(obj, "failure_reason") or "(unknown)"
        refund_id = _field(obj, "id")
        log.error(
            "STRIPE REFUND FAILED: %s %s charge=%s reason=%s refund=%s "
            "— contact the customer to arrange an alternate refund path",
            amt, cur, charge, failure_reason, refund_id,
        )
        return _finish(
            handled=True,
            notes=f"refund FAILED {amt} {cur} charge={charge} reason={failure_reason}",
        )

    customer_id = _field(obj, "customer")
    # Some events expand the customer to a full object instead of a
    # bare id string. Unwrap it.
    if customer_id is not None and not isinstance(customer_id, str):
        customer_id = _field(customer_id, "id")
    # On payment_method.detached the object's `customer` is already null
    # (the detach is what fired the event) — fall back to the diff in
    # previous_attributes which still has the prior customer id.
    if not customer_id and event_type == "payment_method.detached":
        prev = _field(data, "previous_attributes")
        customer_id = _field(prev, "customer")
        if customer_id is not None and not isinstance(customer_id, str):
            customer_id = _field(customer_id, "id")
    if not customer_id:
        # For customer.* events the object IS the customer.
        candidate = _field(obj, "id")
        if isinstance(candidate, str) and candidate.startswith("cus_"):
            customer_id = candidate

    audit.stripe_customer_id = customer_id

    user = (
        db.query(User).filter(User.stripe_customer_id == customer_id).first()
        if customer_id
        else None
    )

    if user is None:
        # Event for a customer we don't have a row for — likely a stale
        # test event or a customer we cleaned up. Acknowledge and move on.
        log.warning("stripe webhook for unknown customer %s", customer_id)
        return _finish(handled=False, notes=f"unknown customer {customer_id}")

    audit.user_id = user.id

    if event_type in ("setup_intent.succeeded", "payment_method.attached"):
        # Card-on-file is decoupled from being on a plan: attaching a card
        # no longer flips payment_status to 'active'. 'active' comes only
        # from invoice.paid (a real subscription charge), so a user can
        # save a card now and pick a plan later. Card presence is read
        # live from Stripe in /status via has_any_payment_method.
        log.info("card attached for user %s (no plan change)", user.id)
    elif event_type == "payment_method.detached":
        # Only matters if they were ON a plan - removing the last card
        # while subscribed means we can't bill, so treat it as canceled.
        # A no-plan user (status 'none') just has no card on file now.
        if user.payment_status in ("active", "past_due"):
            try:
                still_has = billing.has_any_payment_method(customer_id)
            except Exception:
                log.exception("post-detach method check failed for %s", customer_id)
                still_has = True  # err on the side of leaving them active
            if not still_has:
                user.payment_status = "canceled"
    elif event_type == "invoice.paid":
        user.payment_status = "active"
        user.last_billed_at = datetime.now(timezone.utc)
        # Mark the matching UsageRecord rows billed
        db.query(UsageRecord).filter(
            UsageRecord.user_id == user.id, UsageRecord.billed.is_(False)
        ).update({"billed": True})
    elif event_type == "invoice.payment_failed":
        user.payment_status = "past_due"
    elif event_type in ("customer.deleted", "customer.subscription.deleted"):
        user.payment_status = "canceled"
        user.stripe_customer_id = None
    elif event_type == "invoice.upcoming":
        # Stripe fires this ~7 days before an invoice would be charged
        # (we'd configure the lead time on the subscription). For now
        # we just log it. Future: send a heads-up email so the user
        # can update their card if needed before the charge fails.
        log.info(
            "invoice.upcoming for user %s: amount_due=%s %s period_end=%s",
            user.id,
            _field(obj, "amount_due"),
            _field(obj, "currency"),
            _field(obj, "period_end"),
        )

    return _finish(handled=True, notes=f"customer-event for user {user.id}")
