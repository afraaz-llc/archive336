"""Billing math + Stripe SDK wrapper.

Two billing tracks running side by side:

1. **Membership** — a Stripe Subscription for $1/year, started the
   moment the user adds a card. Stripe handles renewal, dunning, and
   cancellation automatically; we just react to its webhook events.
   The subscription's anniversary is the user's signup-with-card date,
   not a fixed calendar day.

2. **Storage** — pay-as-you-go, billed by us via one-off Stripe
   invoices when the unbilled accrual crosses $5. Cron-driven, runs
   monthly on the 3rd. Storing raw bytes per day means pricing changes
   don't require recomputing history.

Storage pricing:
- Storage cost (Backblaze B2): $0.006/GB/month. Markup 5x -> we charge
  $0.030/GB/month (the price we held when storage moved off R2).
- Bill threshold: $5. Below that the accrual rolls over.

Membership pricing:
- Flat $1/year. Floor that covers per-user overhead even for fully
  idle accounts. Stripe takes a cut on the $1 (~$0.33 on cards,
  ~$0.01 on ACH); we accept the slim net since the goal is "stay
  above water," not maximize fee margin.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import stripe


# ---- Pricing constants -----------------------------------------------------
#
# Storage billing source of truth. Read this section before changing anything:
# the rate is defined at the byte-hour level (the smallest unit Cloudflare R2
# actually meters at) and every higher-level rate is *derived* from it. This
# avoids the two historical drift bugs we used to have:
#   1. 720 vs 730.485 hours/month  (we used the 30-day calendar month
#      assumption; Cloudflare bills against the average month length)
#   2. binary GiB vs decimal GB    (we used 2^30 bytes/GB; Cloudflare's
#      $0.015/GB-month is a *decimal* GB = 10^9 bytes)
# Combined, the old constants undercharged users ~5.56% relative to the
# intended 2x markup of Cloudflare's actual cost. Fixed 2026-05-24 by
# anchoring the rate at the byte-hour and deriving the rest.

# Storage cost basis. Video/thumbnail storage is on Backblaze B2 (migrated off
# Cloudflare R2 on 2026-06-03). B2 raised its rate to $6.95/TB (~$0.00695/GB-mo)
# around May 2025; we use $0.007 here, rounding up a hair to stay conservative
# (never under-count our own cost). Update if B2 re-prices.
STORAGE_COST_PER_GB_PER_MONTH_USD = 0.007

# Decimal GB (10^9 bytes) — matches Cloudflare's GB definition for billing.
# NOT binary GiB (2^30 bytes). The two differ by ~7.4%.
BYTES_PER_GB = 1_000_000_000

# Average month length in hours, used by Cloudflare for their per-GB-month
# rate. 365.25 days × 24 hours / 12 months = 730.485 hours/month.
# (We used to assume 720 = a 30-day month, which undercharged by 1.46%.)
HOURS_PER_MONTH_AVG = 730.485

# ---- Per-tier storage price (the source of truth) -------------------------
# Storage is priced near wholesale. Higher tiers pay a lower per-GB rate;
# internal tiers are not charged for storage at all.
STORAGE_PRICE_PER_GB_MONTH_BY_TIER = {
    # Commercial
    "core": 0.0,
    "basic": 0.02,
    "creator": 0.01,
    "studio": 0.0075,
    # Internal (non-paying)
    "partner": 0.0,
    "dev": 0.0,
    "vip": 0.0,
    "admin": 0.0,
}

# Basic is the entry/default commercial tier. The single-value constants below
# represent Basic - they feed the public price endpoint + admin defaults. The
# real per-user charge goes through get_user_storage_markup() + the tier map.
DEFAULT_STORAGE_PRICE_PER_GB_MONTH = STORAGE_PRICE_PER_GB_MONTH_BY_TIER["basic"]

# Markup = price / cost (Basic's). A derived ratio; the clean source is the
# price map above.
MARKUP = DEFAULT_STORAGE_PRICE_PER_GB_MONTH / STORAGE_COST_PER_GB_PER_MONTH_USD

# Our storage cost to us at the smallest billable quantum (byte-hour),
# derived from the per-GB-month rate above (now Backblaze B2's rate).
# Everything user-facing derives from this, so the price stays exact.
STORAGE_COST_USD_PER_BYTE_HOUR = STORAGE_COST_PER_GB_PER_MONTH_USD / (
    BYTES_PER_GB * HOURS_PER_MONTH_AVG
)

# Basic's price per byte-hour (the default). Per-tier prices derive the same
# way from the tier map; for a specific user go through their markup, not this.
PRICE_USD_PER_BYTE_HOUR = STORAGE_COST_USD_PER_BYTE_HOUR * MARKUP

# Public-facing aggregate = Basic's $/decimal GB-month (= $0.02), the entry
# price shown on the pricing page. Per-tier rates live in the map above; don't
# use this for the actual bill cron math - that goes through the user's markup.
PRICE_PER_GB_PER_MONTH_USD = PRICE_USD_PER_BYTE_HOUR * BYTES_PER_GB * HOURS_PER_MONTH_AVG  # = 0.02 (Basic)

# Egress (downloads) is FREE to users. Downloads stream B2 -> Cloudflare Worker
# (dl.archive336.com) -> user, which costs us nothing (Bandwidth
# Alliance free egress), so we don't bill for it. Downloads are auth-gated
# per-user, so abuse isn't a concern. Set a token rate here only if that
# economics ever changes.
DOWNLOAD_PRICE_PER_GB_USD = 0.0

# Don't bother creating a Stripe invoice for less than this dollar
# amount of storage usage. Keep float, convert to cents only when
# calling Stripe. Below this, accrual rolls over to next month.
MIN_INVOICE_USD = 5.00

# Flat annual membership fee. Charged via Stripe Subscription on first
# card-add and renewed yearly on the same anniversary. We don't charge
# this from our cron — Stripe drives the renewal cycle.
ANNUAL_FEE_USD = 1.00

# The day of the month the storage-billing cron runs.
BILLING_DAY_OF_MONTH = 3

# Stripe transaction fee passthrough on small final charges. The
# regular cron only invoices when the storage accrual crosses
# MIN_INVOICE_USD ($5), but at account deletion we want to capture
# *any* non-zero accrual. Stripe's per-charge fee (~2.9% + $0.30) can
# easily exceed the amount on tiny charges, so for amounts below the
# $5 threshold we tack a flat $0.55 fee on so we don't lose money.
# Charges at or above $5 absorb the fee on our side - the math works
# out fine and it's a friendlier final invoice for a leaving user.
SMALL_CHARGE_FEE_USD = 0.55


# ---- Stripe fee schedule (US account, published rates) -------------------
# Used by /api/admin/revenue to compute per-item net amounts across
# payment methods, and anywhere else we need to forecast take-home.
# Source: stripe.com/pricing (US business). Update if Stripe re-prices.

# US-issued cards (standard rate).
STRIPE_FEE_PCT_US_CARD = 0.029
STRIPE_FEE_FIXED_USD = 0.30

# International cards add a 1.5% surcharge over the US base rate.
STRIPE_FEE_PCT_INTL_CARD_SURCHARGE = 0.015

# Currency conversion adds 1% on top when the charge is presented in
# a currency other than the account's default (USD for us).
STRIPE_FEE_PCT_CURRENCY_CONVERSION = 0.01

# ACH Direct Debit: percentage with no fixed fee, capped per transaction.
STRIPE_FEE_PCT_ACH = 0.008
STRIPE_FEE_ACH_CAP_USD = 5.00


def stripe_net_us_card(gross_usd: float) -> float:
    """Net we keep after Stripe's US-card fee. Standard 2.9% + $0.30."""
    return round(
        gross_usd - (gross_usd * STRIPE_FEE_PCT_US_CARD + STRIPE_FEE_FIXED_USD),
        4,
    )


def stripe_net_intl_card_usd(gross_usd: float) -> float:
    """Net for an international card paying in USD. Adds the 1.5%
    international surcharge to the US base rate."""
    pct = STRIPE_FEE_PCT_US_CARD + STRIPE_FEE_PCT_INTL_CARD_SURCHARGE
    return round(gross_usd - (gross_usd * pct + STRIPE_FEE_FIXED_USD), 4)


def stripe_net_intl_card_non_usd(gross_usd: float) -> float:
    """Net for an international card paying in a non-USD currency.
    Adds the 1.5% intl surcharge AND the 1% conversion fee."""
    pct = (
        STRIPE_FEE_PCT_US_CARD
        + STRIPE_FEE_PCT_INTL_CARD_SURCHARGE
        + STRIPE_FEE_PCT_CURRENCY_CONVERSION
    )
    return round(gross_usd - (gross_usd * pct + STRIPE_FEE_FIXED_USD), 4)


def stripe_net_ach(gross_usd: float) -> float:
    """Net for an ACH Direct Debit charge. 0.8% no fixed fee, capped $5."""
    fee = min(gross_usd * STRIPE_FEE_PCT_ACH, STRIPE_FEE_ACH_CAP_USD)
    return round(gross_usd - fee, 4)


# ---- Loss scenarios (refunds + chargebacks) -------------------------------
# Stripe's dispute (chargeback) fee, applied per-dispute regardless of
# the disputed amount or whether we win the dispute.
STRIPE_DISPUTE_FEE_USD = 15.00


def stripe_loss_refund(gross_usd: float) -> float:
    """Net when WE refund a successful charge.

    Stripe keeps the original processing fee on refunds (policy since
    ~2020). The user gets their `gross` back; we eat whatever the
    original fee was. So net = -original_fee. Computed assuming a
    US card was used originally — the most common case at our scale.
    For international cards the loss is ~$0.01-$0.02 worse; for ACH
    refunds the loss is just the ~0.8% (no fixed fee).
    """
    original_fee = gross_usd * STRIPE_FEE_PCT_US_CARD + STRIPE_FEE_FIXED_USD
    return round(-original_fee, 4)


def stripe_loss_chargeback(gross_usd: float) -> float:
    """Net when a charge is disputed and we LOSE the dispute (or don't
    respond). Stripe reverses the `gross` AND charges us the $15
    dispute fee. So net = -(gross + $15). The original processing
    fee is also kept by Stripe but is already baked into the gross
    side of the math (we never received the gross net, so the loss
    is just the gross we briefly had + the dispute fee).

    ACH disputes are different — no $15 fee — but our default
    assumption (US card) is the worst case for cards, which is the
    risk we actually need to plan for.
    """
    return round(-(gross_usd + STRIPE_DISPUTE_FEE_USD), 4)

# ---- Byte-hour billing (the airtight method) ------------------------------
# See docs/STORAGE_BILLING_DESIGN.md. The rate constants themselves now live
# above with the other pricing constants — single source of truth at the
# byte-hour granularity. The function helpers below just apply that rate.

# Default storage markup (Basic's) - the fallback when a charge fn is called
# without an explicit per-user markup. The real per-user value comes from
# get_user_storage_markup(), which resolves the user's tier (and any explicit
# storage_cost_multiplier_override) through the tier price map above.
DEFAULT_STORAGE_MARKUP = MARKUP


# ---- R2 operations billing (the ops-side sibling of byte-hour) ------------
# Storage is one of four R2 billing axes. Operations are two more (Class A
# for writes/lists/multipart, Class B for reads/heads). The fourth is
# egress, which is free on R2. See docs/CLOUDFLARE_AUDIT.md §13 for the
# full breakdown and the audit decision to bill ops per-user at the same
# 2× markup as storage, ignoring the account-wide free tier (which
# becomes pure platform margin until exhausted).
#
# Standard storage class. Infrequent Access is 2× / 2.5× respectively
# AND has no free tier — we lock that down separately in Phase F.
# CORRECTED to Backblaze. These were Cloudflare R2's published rates
# ($4.50 / $0.36 per million) and survived the move to B2 unnoticed,
# because ops are platform-absorbed and never reach a customer invoice -
# so nothing user-facing ever contradicted them. They were only wrong on
# the admin margin dashboard, where they invented a cost we do not pay.
#
# Backblaze bills pay-as-you-go Class A, B and C transactions at ZERO.
# Every call this app makes is one of those: PUT/COPY (A), GET/HEAD (B),
# LIST (C). Only Class D is chargeable ($0.004 per 10,000, first 2,500
# per day free), and nothing here issues one. Verified against
# backblaze.com/cloud-storage/pricing, 2026-07.
#
# Kept as named constants rather than deleted so the day a Class D call
# appears there is an obvious place to price it.
R2_CLASS_A_USD_PER_MILLION = 0.0   # B2 Class A (PUT, COPY, multipart) - free
R2_CLASS_B_USD_PER_MILLION = 0.0   # B2 Class B (GET, HEAD) - free

# Free tier per account per month (Standard storage class only). Documented
# here for reference and used by the canary alerts — billing math itself
# ignores the free tier (see docs/CLOUDFLARE_AUDIT.md §14 for why).
R2_CLASS_A_FREE_TIER_PER_MONTH = 1_000_000
R2_CLASS_B_FREE_TIER_PER_MONTH = 10_000_000


def ops_to_r2_cost_usd(class_a_count: int, class_b_count: int) -> float:
    """Raw storage-provider cost (no markup) for the given op counts.

    Zero on Backblaze, which is what we are on - see the constants above.
    The function stays because the op COUNTS are still worth surfacing as
    telemetry, and because a future Class D call would be priced here.

    Pre-free-tier, pre-rounding. Cloudflare actually bills in whole-
    million tiers per the audit doc — we don't model that rounding
    because the per-user share is below any plausible tier boundary.
    The platform absorbs the rounding-up cost.
    """
    a_usd = (class_a_count / 1_000_000.0) * R2_CLASS_A_USD_PER_MILLION
    b_usd = (class_b_count / 1_000_000.0) * R2_CLASS_B_USD_PER_MILLION
    return a_usd + b_usd


def ops_to_user_charge_usd(
    class_a_count: int,
    class_b_count: int,
    markup: float = DEFAULT_STORAGE_MARKUP,
) -> float:
    """What we would charge a user for their op counts.

    Currently always 0.0: Backblaze does not bill the transaction classes
    this app issues. Retained (rather than removed) because the shape is
    right if that ever changes, and because the admin dashboard reads it.
    Note ops are platform-absorbed today - bill.py bills storage only.
    """
    return ops_to_r2_cost_usd(class_a_count, class_b_count) * markup


# ---- Stripe SDK wrapper ----------------------------------------------------


def _configure() -> None:
    key = os.environ.get("STRIPE_SECRET_KEY")
    if not key:
        raise RuntimeError("STRIPE_SECRET_KEY missing from .env")
    stripe.api_key = key
    # Pin a recent API version so we don't get surprised by silent changes.
    # Bumped to dahlia in May 2026 when Stripe activated live mode for this
    # account and no longer offered the acacia version for new webhook
    # endpoints. The webhook destination in the dashboard sends dahlia-
    # shaped events; matching the SDK pin here keeps inbound + outbound
    # consistent.
    stripe.api_version = "2026-04-22.dahlia"


def get_or_create_customer(
    email: str, username: str, existing_customer_id: Optional[str] = None
) -> str:
    """Return a Stripe Customer id for the given user, creating one if needed."""
    _configure()
    if existing_customer_id:
        try:
            stripe.Customer.retrieve(existing_customer_id)
            return existing_customer_id
        except stripe.error.InvalidRequestError:
            # Customer was deleted on Stripe's side — create fresh.
            pass
    customer = stripe.Customer.create(
        email=email,
        name=username,
        metadata={"aether_username": username},
    )
    return customer.id


def create_setup_intent(customer_id: str) -> dict:
    """Create a SetupIntent so the frontend can collect a card.

    SetupIntent is Stripe's primitive for "save a card without charging
    it now." Returns the client_secret the frontend feeds into Stripe
    Elements.
    """
    _configure()
    intent = stripe.SetupIntent.create(
        customer=customer_id,
        payment_method_types=["card"],
        usage="off_session",  # we'll charge them later from the cron
    )
    return {
        "clientSecret": intent.client_secret,
        "publishableKey": os.environ.get("STRIPE_PUBLISHABLE_KEY", ""),
    }


def create_billing_portal_session(customer_id: str, return_url: str) -> str:
    """Return a URL to Stripe's hosted customer portal.

    User clicks the link in our Settings page → Stripe handles card
    management, payment history, etc. → user clicks "Back" → comes back
    to ``return_url`` (typically /settings).
    """
    _configure()
    session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=return_url,
    )
    return session.url


def get_active_membership_subscription(customer_id: str):
    """Return the customer's active/past_due/trialing membership
    subscription if any, else None.

    'Active' here means anything other than canceled/unpaid/incomplete
    that we'd want to leave alone. Used to avoid double-creating a
    subscription on a customer who already has one.
    """
    _configure()
    subs = stripe.Subscription.list(customer=customer_id, status="all", limit=10)
    membership_price = os.environ.get("STRIPE_MEMBERSHIP_PRICE_ID", "")
    for s in subs.data:
        if s.status in ("canceled", "incomplete_expired"):
            continue
        # Match on the membership price ID — a customer could in
        # principle have other subscriptions later (e.g. different
        # plan tiers), so we filter to the one we care about.
        for item in s["items"].data:
            if item.price.id == membership_price:
                return s
    return None


def bill_outstanding_now(
    customer_id: str,
    amount_cents: int,
    description: str,
    period_start: datetime,
    period_end: datetime,
) -> dict:
    """Synchronously charge the customer's card for an outstanding
    amount and return the resulting invoice's status. Raises
    stripe.error.CardError on a decline.

    Built for the account-deletion flow: we want a yes/no answer
    *before* deciding whether to wipe the customer. The cron's
    add_invoice_item + finalize_invoice flow is async (auto_advance
    schedules a background charge attempt) and bypasses the $5
    minimum-bill threshold - neither is right here. The user is
    leaving, so we want to capture *any* non-zero accrual now or not
    at all.

    The flow:
      1. InvoiceItem.create - stages the line item.
      2. Invoice.create with auto_advance=False - we'll drive it
         manually instead of letting Stripe's scheduler do it.
      3. Invoice.finalize_invoice - finalizes the line items into a
         charge-able invoice.
      4. Invoice.pay - blocks until the charge completes; raises
         CardError on decline.
    """
    _configure()
    stripe.InvoiceItem.create(
        customer=customer_id,
        amount=amount_cents,
        currency="usd",
        description=description,
        period={
            "start": int(period_start.timestamp()),
            "end": int(period_end.timestamp()),
        },
    )
    invoice = stripe.Invoice.create(
        customer=customer_id,
        auto_advance=False,
        collection_method="charge_automatically",
    )
    invoice = stripe.Invoice.finalize_invoice(invoice.id)
    invoice = stripe.Invoice.pay(invoice.id)
    return {
        "invoice_id": invoice.id,
        "status": invoice.status,
        "amount_paid_cents": invoice.amount_paid,
    }


def delete_customer(customer_id: str) -> None:
    """Permanently delete the Stripe customer and everything attached.

    Cascades automatically on Stripe's side: cancels active
    subscriptions, voids open invoices, detaches payment methods.
    Idempotent - if the customer is already gone, swallows the error.
    Used by the user account-deletion flow so we don't leave dangling
    Stripe resources after the local user row is removed.
    """
    _configure()
    try:
        stripe.Customer.delete(customer_id)
    except stripe.error.InvalidRequestError as e:
        # 'No such customer' / already deleted - nothing to do.
        msg = str(e).lower()
        if "no such customer" in msg or "already" in msg:
            return
        raise


def _ensure_default_payment_method(customer_id: str) -> Optional[str]:
    """Make sure the Stripe customer has a default payment method set
    on `invoice_settings.default_payment_method`. Returns the PM id
    that's now the default, or None if the customer has no payment
    methods attached.

    Stripe's SetupIntent flow attaches the card to the customer but
    doesn't promote it to default — every subsequent Subscription /
    Invoice call would otherwise fail with "no default payment method".
    Idempotent: if a default is already set, returns it unchanged.
    """
    _configure()
    customer = stripe.Customer.retrieve(customer_id)
    invoice_settings = getattr(customer, "invoice_settings", None)
    current_default = (
        getattr(invoice_settings, "default_payment_method", None)
        if invoice_settings
        else None
    )
    if current_default:
        return current_default

    pms = stripe.PaymentMethod.list(customer=customer_id, limit=1)
    if not pms.data:
        return None
    pm = pms.data[0]
    stripe.Customer.modify(
        customer_id,
        invoice_settings={"default_payment_method": pm.id},
    )
    return pm.id


def create_membership_subscription(customer_id: str) -> dict:
    """Start a $1/year membership for the customer.

    Stripe immediately charges the customer's default payment method
    (or the most-recently-attached one as fallback) for the first $1
    and schedules renewals one year out. Webhook events
    (invoice.paid / invoice.payment_failed) keep our payment_status
    in sync going forward — no anniversary cron needed on our side.

    Idempotent: if the customer already has an active membership we
    return the existing one rather than creating a duplicate.
    """
    _configure()
    price_id = os.environ.get("STRIPE_MEMBERSHIP_PRICE_ID", "").strip()
    if not price_id:
        raise RuntimeError(
            "STRIPE_MEMBERSHIP_PRICE_ID missing from .env — create the "
            "membership product in Stripe and add the price ID."
        )

    existing = get_active_membership_subscription(customer_id)
    if existing is not None:
        return {
            "subscription_id": existing.id,
            "status": existing.status,
            "created": False,
        }

    # Stripe Subscriptions need a default payment method on the
    # *customer* (or passed in explicitly) — attaching a PaymentMethod
    # via SetupIntent does not auto-set it as default. So before we
    # create the subscription, grab the most-recently-attached PM and
    # mark it default. Without this we get:
    #   "This customer has no attached payment source or default
    #    payment method."
    pm_id = _ensure_default_payment_method(customer_id)
    if pm_id is None:
        raise RuntimeError(
            "Customer has no payment methods attached — caller should "
            "have validated this before reaching create_membership_subscription."
        )

    sub = stripe.Subscription.create(
        customer=customer_id,
        items=[{"price": price_id}],
        # Belt-and-suspenders: pass the PM id explicitly so we don't
        # depend on Stripe's customer-default lookup, even though we
        # just set it above.
        default_payment_method=pm_id,
        # Charge the customer's default payment method
        # automatically; let Stripe retry on failure per the dunning
        # rules in our Stripe dashboard.
        collection_method="charge_automatically",
        # If the first invoice fails, mark the subscription incomplete
        # so we don't grant access. The user can retry by updating
        # their card via the customer portal.
        payment_behavior="error_if_incomplete",
    )
    return {
        "subscription_id": sub.id,
        "status": sub.status,
        "created": True,
    }


# ----- Multi-tier plan subscriptions (Basic + Creator) -----

# Plan tier -> the .env key holding its Stripe recurring price ID.
PLAN_PRICE_ENV = {
    "basic": "STRIPE_MEMBERSHIP_PRICE_ID",
    "creator": "STRIPE_CREATOR_PRICE_ID",
}


def _plan_price_ids() -> set[str]:
    """The set of Stripe price IDs that represent one of our plans."""
    out: set[str] = set()
    for env_key in PLAN_PRICE_ENV.values():
        v = os.environ.get(env_key, "").strip()
        if v:
            out.add(v)
    return out


def get_active_plan_subscription(customer_id: str):
    """The customer's active subscription on ANY ARCHIVE336 plan price
    (Basic or Creator), else None. Tier-aware generalization of
    get_active_membership_subscription."""
    _configure()
    ours = _plan_price_ids()
    subs = stripe.Subscription.list(customer=customer_id, status="all", limit=20)
    for s in subs.data:
        if s.status in ("canceled", "incomplete_expired"):
            continue
        for item in s["items"].data:
            if item.price.id in ours:
                return s
    return None


def set_plan_subscription(customer_id: str, tier: str) -> dict:
    """Move the customer onto the given plan tier's subscription.

    A clean cancel-and-resubscribe (no proration), per the product
    decision: cancel any existing ARCHIVE336 plan subscription, then
    create the new tier's. Requires an attached payment method.
    Idempotent: if already on this tier's price, returns it untouched.
    """
    _configure()
    env_key = PLAN_PRICE_ENV.get(tier)
    if not env_key:
        raise RuntimeError(f"No Stripe price configured for plan {tier!r}")
    price_id = os.environ.get(env_key, "").strip()
    if not price_id:
        raise RuntimeError(f"{env_key} missing from .env")

    pm_id = _ensure_default_payment_method(customer_id)
    if pm_id is None:
        raise RuntimeError("Customer has no payment methods attached")

    existing = get_active_plan_subscription(customer_id)
    if existing is not None:
        already = any(i.price.id == price_id for i in existing["items"].data)
        if already:
            return {
                "subscription_id": existing.id,
                "status": existing.status,
                "created": False,
            }
        # Clean switch: drop the old plan sub before starting the new one.
        stripe.Subscription.cancel(existing.id)

    sub = stripe.Subscription.create(
        customer=customer_id,
        items=[{"price": price_id}],
        default_payment_method=pm_id,
        collection_method="charge_automatically",
        payment_behavior="error_if_incomplete",
    )
    return {
        "subscription_id": sub.id,
        "status": sub.status,
        "created": True,
    }


def cancel_active_plan_subscription(customer_id: str) -> bool:
    """Cancel the customer's active plan subscription, if any. Returns True
    if one was canceled, False if there was none. Used by the account
    'Cancel plan' flow — stops the recurring membership charge immediately.
    """
    _configure()
    existing = get_active_plan_subscription(customer_id)
    if existing is None:
        return False
    stripe.Subscription.cancel(existing.id)
    return True


def has_any_payment_method(customer_id: str) -> bool:
    """Whether the customer has any chargeable payment method attached.

    SetupIntent attaches a payment method to the customer but doesn't
    set it as the invoice_settings.default_payment_method automatically
    — that would always be empty on first add. We just check
    PaymentMethod.list instead. Stripe's invoicing falls back to the
    most-recently-attached method when no default is set, so any
    attached method is enough for billing to work.
    """
    _configure()
    methods = stripe.PaymentMethod.list(customer=customer_id, limit=1)
    return len(methods.data) > 0


def get_default_payment_method(customer_id: str) -> Optional[dict]:
    """Summarize the payment method that will be charged, or None if the
    customer has none on file. Handles cards AND non-card methods (a user
    can attach an ACH bank account via the Stripe billing portal).

    Prefers invoice_settings.default_payment_method, else the most-recently
    attached method (any type). Shape:
      card             -> {type:"card", brand, last4, expMonth, expYear}
      us_bank_account  -> {type:"us_bank_account", bankName, last4}
      anything else    -> {type:<stripe type>, last4?}
    """
    _configure()
    pm = None
    try:
        customer = stripe.Customer.retrieve(customer_id)
        invoice_settings = getattr(customer, "invoice_settings", None)
        default_pm_id = (
            getattr(invoice_settings, "default_payment_method", None)
            if invoice_settings
            else None
        )
        if default_pm_id:
            pm = stripe.PaymentMethod.retrieve(default_pm_id)
    except Exception:
        pm = None
    if pm is None:
        methods = stripe.PaymentMethod.list(customer=customer_id, limit=1)
        pm = methods.data[0] if methods.data else None
    if pm is None:
        return None

    pm_type = getattr(pm, "type", None)
    if pm_type == "card":
        card = getattr(pm, "card", None)
        return {
            "type": "card",
            "brand": getattr(card, "brand", None) if card else None,
            "last4": getattr(card, "last4", None) if card else None,
            "expMonth": getattr(card, "exp_month", None) if card else None,
            "expYear": getattr(card, "exp_year", None) if card else None,
        }
    if pm_type == "us_bank_account":
        bank = getattr(pm, "us_bank_account", None)
        return {
            "type": "us_bank_account",
            "bankName": getattr(bank, "bank_name", None) if bank else None,
            "last4": getattr(bank, "last4", None) if bank else None,
        }
    # Any other type (sepa_debit, link, cashapp, …): a generic summary.
    details = getattr(pm, pm_type, None) if pm_type else None
    return {
        "type": pm_type or "unknown",
        "last4": getattr(details, "last4", None) if details else None,
    }


def list_invoices(customer_id: str, limit: int = 12) -> list:
    """Return the customer's recent Stripe invoices, newest first, for the
    in-app billing-history section. Each dict: {id, number, created (unix),
    total (cents), amountPaid (cents), currency, status, hostedUrl, pdfUrl}.
    """
    _configure()
    invoices = stripe.Invoice.list(customer=customer_id, limit=limit)
    out = []
    for inv in invoices.data:
        out.append(
            {
                "id": inv.id,
                "number": getattr(inv, "number", None),
                "created": getattr(inv, "created", None),
                "total": getattr(inv, "total", None),
                "amountPaid": getattr(inv, "amount_paid", None),
                "currency": getattr(inv, "currency", None),
                "status": getattr(inv, "status", None),
                "hostedUrl": getattr(inv, "hosted_invoice_url", None),
                "pdfUrl": getattr(inv, "invoice_pdf", None),
            }
        )
    return out


def add_invoice_item(
    customer_id: str,
    amount_cents: int,
    description: str,
    period_start: Optional[datetime] = None,
    period_end: Optional[datetime] = None,
) -> None:
    """Stage one line item against the customer.

    The item sits as "pending" on the customer until the next call to
    finalize_invoice() pulls all pending items into a draft invoice.
    Multiple calls to add_invoice_item() before one finalize_invoice()
    means the invoice has multiple lines (e.g. storage + annual fee).
    """
    _configure()
    kwargs: dict = {
        "customer": customer_id,
        "amount": amount_cents,
        "currency": "usd",
        "description": description,
    }
    if period_start is not None and period_end is not None:
        kwargs["period"] = {
            "start": int(period_start.timestamp()),
            "end": int(period_end.timestamp()),
        }
    stripe.InvoiceItem.create(**kwargs)


def finalize_invoice(customer_id: str) -> dict:
    """Pull all pending invoice items into a draft and charge it.

    Uses Stripe's auto-advance flow so the invoice is finalized and
    payment attempted in one go — no manual "send invoice" step. The
    webhook handles the paid/failed outcome.
    """
    _configure()
    invoice = stripe.Invoice.create(
        customer=customer_id,
        auto_advance=True,
        collection_method="charge_automatically",
    )
    invoice = stripe.Invoice.finalize_invoice(invoice.id)
    return {
        "invoice_id": invoice.id,
        "status": invoice.status,
        "amount_cents": invoice.amount_due,
        "hosted_url": invoice.hosted_invoice_url,
    }


# ---- Pricing math ----------------------------------------------------------


def gb_days_to_usd(gb_days: float) -> float:
    """Convert "GB stored across N days" into a dollar charge.

    Legacy helper kept for the cross-check endpoint that compares the
    new byte-hour ledger to the old UsageRecord daily-snapshot path.
    The math routes through the byte-hour rate so both sides agree.

    1 GB-day = 10^9 bytes × 24 hours = 2.4 × 10^10 byte-hours.
    """
    byte_hours = gb_days * BYTES_PER_GB * 24.0
    return byte_hours_to_user_charge_usd(byte_hours)


def bytes_to_gb(byte_count: int) -> float:
    """Bytes → GB (decimal, not GiB — matches how R2 prices its storage)."""
    return byte_count / BYTES_PER_GB


def byte_hours_to_storage_cost_usd(byte_hours: float) -> float:
    """What R2 charges us for ``byte_hours`` of storage at the rate sheet.

    Computed at the smallest billable quantum (the byte-hour) to avoid
    any intermediate GB-hour / GB-month rounding. Exact match to what
    Cloudflare integrates internally for that user's objects.
    """
    return byte_hours * STORAGE_COST_USD_PER_BYTE_HOUR


def byte_hours_to_user_charge_usd(
    byte_hours: float, markup: float = DEFAULT_STORAGE_MARKUP
) -> float:
    """What we charge a user for ``byte_hours``: R2's cost × markup."""
    return byte_hours * STORAGE_COST_USD_PER_BYTE_HOUR * markup


def storage_markup_for_tier(tier: str) -> float:
    """Markup (price / cost) for a tier, from the per-tier price map.

    Unknown tiers fall back to Basic's rate so we charge rather than ever
    accidentally serve free storage.
    """
    price = STORAGE_PRICE_PER_GB_MONTH_BY_TIER.get(
        tier, DEFAULT_STORAGE_PRICE_PER_GB_MONTH
    )
    return price / STORAGE_COST_PER_GB_PER_MONTH_USD


def get_user_storage_markup(user) -> float:
    """Effective storage markup for this user.

    Priority: an explicit per-user override > the user's tier rate. The tier is
    read through ``effective_tier`` so any tier indirection is applied
    consistently with every other gate.
    """
    override = getattr(user, "storage_cost_multiplier_override", None)
    if override:
        return float(override)
    tier = getattr(user, "effective_tier", None) or "basic"
    return storage_markup_for_tier(tier)


def compute_user_byte_hours(
    db, user_id: str, start: datetime, end: datetime
) -> float:
    """Sum exact byte-hours of storage for ``user_id`` across [start, end].

    The integral matches what Cloudflare R2 actually charges us for
    that user's objects — uploaded_at and deleted_at on each
    StorageObject row are clamped to the window and multiplied by
    (bytes + metadata_bytes) per hour.

    Returns 0.0 if the user has no overlapping objects in the window.
    See docs/STORAGE_BILLING_DESIGN.md for the full derivation.
    """
    # Imported lazily to avoid a circular dep at module load time.
    from app.models import StorageObject  # noqa: WPS433
    from sqlalchemy import or_  # noqa: WPS433

    rows = (
        db.query(StorageObject)
        .filter(
            StorageObject.user_id == user_id,
            StorageObject.uploaded_at < end,
            or_(
                StorageObject.deleted_at.is_(None),
                StorageObject.deleted_at > start,
            ),
        )
        .all()
    )
    total = 0.0
    for r in rows:
        # SQLite stores naive datetimes; coerce to UTC-aware for safe
        # arithmetic against our (always tz-aware) start/end.
        up = r.uploaded_at
        if up.tzinfo is None:
            up = up.replace(tzinfo=timezone.utc)
        de = r.deleted_at
        if de is not None and de.tzinfo is None:
            de = de.replace(tzinfo=timezone.utc)

        effective_start = max(up, start)
        effective_end = min(de or end, end)
        if effective_end > effective_start:
            hours = (effective_end - effective_start).total_seconds() / 3600.0
            total += (r.bytes + r.metadata_bytes) * hours
    return total


def compute_user_byte_hours_v2(
    db, user_id: str, start: datetime, end: datetime
) -> float:
    """Shared-pool replacement for ``compute_user_byte_hours``.

    For each Video the user is billable for during [start, end],
    integrates ``bytes_stored × overlap_hours``. A user is billable
    for a video iff they have the relationship that put those bytes
    in the archive for them:

      open   (public / age_restricted)            -> UserChannelSubscription
      sealed (private / unlisted / members-only)  -> ChannelOwnership

    Unlisted is sealed, not open: it is link-only on YouTube, so
    visibility_for_privacy() keeps it owner-only. See models.py's
    _OPEN_AT_CAPTURE.

    Both tiers meter on the SAME boundary: the moment we stopped
    holding the channel's bytes for this user, which is when they
    removed the channel. Billing stops exactly there, not at +30-day
    grace. The grace window lets the user resume access without
    re-downloading, but the company eats the storage cost for that
    period as a UX investment. This matches the remove-channel
    confirmation dialog's promise that the user isn't charged during
    the grace window. (Earlier versions of this function and the legacy
    compute_user_byte_hours both included the grace in the billable
    window; corrected here.)

    ``revoked_at`` deliberately plays no part in the maths. See the
    sealed-tier comment below.

    Notable difference vs. v1: no per-object 80-byte metadata overhead.
    The old model added a 80-byte constant per StorageObject row; the
    new Video table stores the file's actual size only. Practical
    impact is negligible (~$10^-8/user/month at 11 objects).
    """
    # Imported lazily to avoid a circular dep at module load time.
    from sqlalchemy import or_  # noqa: WPS433

    from app.models import (  # noqa: WPS433
        Channel,
        ChannelOwnership,
        UserChannel,
        UserChannelSubscription,
        Video,
    )

    def _aware(dt):
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    total = 0.0

    # ---- Public-ish tiers via active subscription ------------------
    # Bring in every subscription that overlapped with [start, end]
    # at any point during which the user was actually subscribed.
    # No grace extension - billing stops exactly at unsubscribed_at.
    subs = (
        db.query(UserChannelSubscription)
        .filter(
            UserChannelSubscription.user_id == user_id,
            UserChannelSubscription.subscribed_at < end,
            or_(
                UserChannelSubscription.unsubscribed_at.is_(None),
                UserChannelSubscription.unsubscribed_at > start,
            ),
        )
        .all()
    )

    for sub in subs:
        sub_started = _aware(sub.subscribed_at)
        if sub.unsubscribed_at is not None:
            sub_ended = _aware(sub.unsubscribed_at)
        else:
            sub_ended = None
        window_start = max(sub_started, start)
        window_end = end if sub_ended is None else min(sub_ended, end)
        if window_end <= window_start:
            continue

        # Open videos count toward the subscriber. Visibility is frozen at
        # capture, so a video grabbed while public keeps counting even
        # after YouTube privates it. Members-only is sealed now (owner
        # tier), handled in the ownership loop below.
        videos = (
            db.query(Video)
            .filter(
                Video.channel_id == sub.channel_id,
                Video.bytes_stored.is_not(None),
                Video.bytes_stored > 0,
                Video.visibility == "open",
            )
            .all()
        )

        for v in videos:
            v_in = _aware(v.synced_at) or _aware(v.created_at)
            if v_in is None:
                continue
            v_start = max(v_in, window_start)
            if window_end > v_start:
                hours = (window_end - v_start).total_seconds() / 3600.0
                total += v.bytes_stored * hours

    # ---- Sealed tier via ChannelOwnership, metered like storage ----
    # Ownership answers WHOSE bytes these are; it does not bound the
    # bill. Storage billing follows storage, not permission:
    #
    #   Revoking authentication is a permission change. It stops NEW
    #   sealed videos being discovered and synced. It deletes nothing -
    #   every sealed file we already hold stays archived, watchable,
    #   and downloadable, and Backblaze keeps charging us for it every
    #   hour. So the meter keeps running. Ending the window at
    #   revoked_at (which this used to do) handed the user unlimited
    #   permanent free storage the moment they pressed Revoke.
    #
    # The action that stops the bill is REMOVING THE CHANNEL, which
    # soft-deletes the subscription and starts the 30-day grace before
    # purge. That is the same boundary the open tier uses above, so
    # both tiers now stop together, at the moment the user tells us to
    # stop holding the files. Do not re-point this at revoked_at.
    #
    # Revoked rows are therefore included, which also makes the old
    # revoke-then-re-authenticate double-charge moot: there is no
    # longer a gap in the window to re-bill, because there is no
    # longer a gap.
    owns = (
        db.query(ChannelOwnership)
        .filter(
            ChannelOwnership.user_id == user_id,
            ChannelOwnership.authenticated_at < end,
        )
        .all()
    )

    # Subscription end per channel FOR THIS USER, which is what bounds
    # the sealed window. Fetched for every subscription the user has,
    # including ones that ended before ``start`` - those must still
    # close the window, not fall back to "never ended".
    #
    # PRESENCE IN THIS DICT IS LOAD-BEARING, and is not the same
    # question as the value being None. Absent = the user has no
    # subscription row for the channel at all. Present with None = the
    # row exists and is still active. Reading this with .get() collapsed
    # the two into "nothing has ended the storage yet", so an owner who
    # never subscribed - a state access.py explicitly supports - got a
    # sealed meter that nothing could ever stop. Look the key up with
    # ``in``, never with .get().
    sub_ends = {
        s.channel_id: _aware(s.unsubscribed_at)
        for s in db.query(UserChannelSubscription)
        .filter(UserChannelSubscription.user_id == user_id)
        .all()
    }

    # Fallback close boundary for owners with no subscription row:
    # UserChannel.removed_at, the legacy per-user tracking row. That
    # timestamp is exactly when the remove-channel handler calls
    # storage_ledger.propagate_channel_soft_delete, i.e. the instant
    # every StorageObject for the channel gets its deleted_at set and
    # we stop holding those bytes for this user. Reading removed_at is
    # the same signal the ledger is driven by, one query instead of
    # re-deriving every storage key per channel. Restores clear it, and
    # propagate_channel_restore clears the ledger with it, so the two
    # stay in step.
    legacy_ends: dict = {}
    unsubscribed_owns = [
        own.channel_id for own in owns if own.channel_id not in sub_ends
    ]
    if unsubscribed_owns:
        # ChannelOwnership.channel_id is the internal Channel.id;
        # UserChannel is keyed by the YouTube UC id, so map across.
        yt_ids = dict(
            db.query(Channel.id, Channel.youtube_id)
            .filter(Channel.id.in_(unsubscribed_owns))
            .all()
        )
        if yt_ids:
            removed = {
                cid: _aware(rem)
                for cid, rem in db.query(
                    UserChannel.channel_id, UserChannel.removed_at
                ).filter(
                    UserChannel.user_id == user_id,
                    UserChannel.channel_id.in_(list(yt_ids.values())),
                )
            }
            for cid in unsubscribed_owns:
                yt = yt_ids.get(cid)
                if yt in removed:
                    legacy_ends[cid] = removed[yt]

    for own in owns:
        if own.channel_id in sub_ends:
            own_ended = sub_ends[own.channel_id]
        elif own.channel_id in legacy_ends:
            own_ended = legacy_ends[own.channel_id]
        else:
            # Neither row exists, so this user never asked us to hold
            # this channel and we are storing nothing on their behalf
            # here. Ownership alone is a permission, not a storage
            # relationship, and billing an owner for bytes they never
            # asked for would be an overcharge with no way to stop it.
            # The bytes themselves are not free: ensure_video()
            # subscribes whoever synced them, so the owner who actually
            # pulled the files is metered on their own subscription.
            continue

        own_started = _aware(own.authenticated_at)
        window_start = max(own_started, start)
        window_end = end if own_ended is None else min(own_ended, end)
        if window_end <= window_start:
            continue

        videos = (
            db.query(Video)
            .filter(
                Video.channel_id == own.channel_id,
                Video.bytes_stored.is_not(None),
                Video.bytes_stored > 0,
                Video.visibility == "sealed",
            )
            .all()
        )
        for v in videos:
            v_in = _aware(v.synced_at) or _aware(v.created_at)
            if v_in is None:
                continue
            v_start = max(v_in, window_start)
            if window_end > v_start:
                hours = (window_end - v_start).total_seconds() / 3600.0
                total += v.bytes_stored * hours

    return total


def compute_user_ops_counts(
    db, user_id: str, start: datetime, end: datetime
) -> tuple[int, int]:
    """Sum Class A + Class B op counts for ``user_id`` across [start, end).

    Reads from R2OperationLog. Each row is one (subject, bucket,
    op_class, day) counter; we sum the rows whose ``day`` falls in
    the half-open window.

    Returns (class_a_count, class_b_count). Both zero if the user
    has no recorded ops in the window (e.g. brand new user, or one
    whose only activity was free ops like DeleteObject).

    Bucket is currently ignored — every recorded op is summed
    regardless of which bucket it hit. In practice users only touch
    the user-content bucket; backups bucket ops are recorded against
    the platform sentinel and never appear on a real user's bill.
    """
    # Imported lazily to avoid a circular dep at module load time.
    from app.models import R2OperationLog  # noqa: WPS433
    from sqlalchemy import func  # noqa: WPS433

    rows = (
        db.query(R2OperationLog.op_class, func.sum(R2OperationLog.count))
        .filter(
            R2OperationLog.subject == user_id,
            R2OperationLog.day >= start,
            R2OperationLog.day < end,
        )
        .group_by(R2OperationLog.op_class)
        .all()
    )
    out = {"A": 0, "B": 0}
    for cls, total in rows:
        if cls in out:
            out[cls] = int(total or 0)
    return out["A"], out["B"]


def compute_user_ops_charge_usd(
    db,
    user_id: str,
    start: datetime,
    end: datetime,
    markup: float = DEFAULT_STORAGE_MARKUP,
) -> float:
    """Total dollars to charge ``user_id`` for ops in [start, end).

    Convenience wrapper over ``compute_user_ops_counts`` +
    ``ops_to_user_charge_usd``. Free tier is ignored at the user
    level — that's platform margin (see docs/CLOUDFLARE_AUDIT.md §14).
    """
    a, b = compute_user_ops_counts(db, user_id, start, end)
    return ops_to_user_charge_usd(a, b, markup)


def should_bill_now(unbilled_usd: float, today: datetime) -> bool:
    """Decide whether to invoice this user during today's monthly run.

    Combined-threshold check: ``unbilled_usd`` should be the SUM of
    the storage charge AND the ops charge for the period. We invoice
    when the combined total has crossed the $5 threshold. Membership
    renewal is driven by Stripe's subscription cycle, not our cron —
    see create_membership_subscription.

    The `today` argument is unused now but kept in the signature in
    case future rules (e.g. "force-bill on month-end of fiscal year")
    need it.
    """
    del today
    return unbilled_usd >= MIN_INVOICE_USD


# Cache the Stripe-side lifetime revenue figure with a 24-hour TTL.
# Background: admin_billing_snapshot used to walk every paid invoice
# in account history (via auto_paging_iter) on every /admin/billing
# hit just to compute lifetime revenue. At any non-trivial invoice
# volume that's thousands of Stripe API calls per panel render, hits
# rate limits, and adds seconds of latency.
#
# The compromise: lifetime revenue moves slowly (only goes up, and
# only by the day's paid invoices), so a 24-hour stale cache is
# acceptable for an admin metric. The shorter-window 30d/90d numbers
# walk live each call but are bounded by the created[gte] filter so
# they don't fan out across history either way.
#
# This is option (b) from the original audit (in-process LRU+TTL).
# Option (a) was a daily roll-up table backed by a cron job — more
# robust against process restarts and multi-worker fanout, but lots
# more moving pieces. Pick (a) when we cross ~10k paid invoices or
# move to multi-worker uvicorn. For now the cache buys ~99.99%
# headroom against the previous fan-out cost.
#
# Thread-safety: FastAPI inside uvicorn is single-process by default
# in our deploy, but we still take a lock around the cache write so
# concurrent /billing hits during the warm-up window don't both fan
# out to Stripe.
_LIFETIME_REVENUE_TTL_SECONDS = 24 * 60 * 60
_lifetime_revenue_cache: dict = {
    "value_cents": None,
    "fetched_at": 0.0,
}
_lifetime_revenue_lock = threading.Lock()


def _compute_lifetime_revenue_cents() -> int:
    """Walk every paid invoice in Stripe history to compute lifetime
    revenue in cents. Cold path — only called when the cache misses.
    Each ``auto_paging_iter()`` page is a Stripe API call so this can
    take seconds and consume hundreds of API requests for accounts
    with substantial invoice history. Keep it behind the TTL cache.
    """
    total_cents = 0
    inv_iter = stripe.Invoice.list(limit=100, status="paid")
    for inv in inv_iter.auto_paging_iter():
        total_cents += inv.amount_paid or 0
    return total_cents


def get_lifetime_revenue_cents(force_refresh: bool = False) -> int:
    """Return the cached lifetime revenue or refresh it when stale.

    The cached value is invalidated after _LIFETIME_REVENUE_TTL_SECONDS
    (24 hours). Pass ``force_refresh=True`` from an admin tool to bypass
    the cache; we don't currently expose a UI for that but the option
    exists for ad-hoc recomputation if a webhook handler ever wants to
    nudge the cached number forward.
    """
    now = time.monotonic()
    cached = _lifetime_revenue_cache["value_cents"]
    age = now - _lifetime_revenue_cache["fetched_at"]
    if (
        not force_refresh
        and cached is not None
        and age < _LIFETIME_REVENUE_TTL_SECONDS
    ):
        return cached

    with _lifetime_revenue_lock:
        # Re-check inside the lock — another thread may have refreshed
        # while we were waiting for the lock.
        now2 = time.monotonic()
        cached2 = _lifetime_revenue_cache["value_cents"]
        age2 = now2 - _lifetime_revenue_cache["fetched_at"]
        if (
            not force_refresh
            and cached2 is not None
            and age2 < _LIFETIME_REVENUE_TTL_SECONDS
        ):
            return cached2

        value = _compute_lifetime_revenue_cents()
        _lifetime_revenue_cache["value_cents"] = value
        _lifetime_revenue_cache["fetched_at"] = now2
        return value


def admin_billing_snapshot(membership_price_id: str) -> dict:
    """Aggregate Stripe-side billing state for the /admin Billing tab.

    Pulls subscriptions + recent invoices straight from Stripe so the
    panel reflects what Stripe actually knows. The rolling-window
    30d/90d revenue numbers walk live (filtered server-side by
    ``created[gte]`` so the page size is bounded); the lifetime number
    is served from a 24-hour cache because it would otherwise force a
    full account-history walk on every call.

    Returns:
      subscriptions: counts by status + computed monthly recurring
                     revenue from active+past_due memberships
                     (annual amount / 12).
      revenue:       paid-invoice totals over rolling windows.
      invoices:      most recent N invoices with status, amount,
                     hosted URL, and ARCHIVE336-side customer label.
    """
    _configure()

    # ---- Subscriptions ---------------------------------------------------
    # status='all' returns every status; we bucket client-side. limit=100
    # is plenty for the early phase — paginate later if subscriber count
    # crosses that.
    subs_resp = stripe.Subscription.list(
        price=membership_price_id, status="all", limit=100
    )
    counts = {"active": 0, "past_due": 0, "canceled": 0, "trialing": 0, "other": 0}
    mrr_cents = 0
    for s in subs_resp.data:
        st = s.status
        if st in counts:
            counts[st] += 1
        else:
            counts["other"] += 1
        # MRR: convert annual subscriptions to monthly. Skip canceled
        # ones — they don't contribute going-forward revenue.
        if st in ("active", "past_due", "trialing"):
            for item in s["items"].data:
                amt = item.price.unit_amount or 0
                interval = item.price.recurring.interval if item.price.recurring else None
                count = item.price.recurring.interval_count if item.price.recurring else 1
                if interval == "year":
                    mrr_cents += amt // (12 * (count or 1))
                elif interval == "month":
                    mrr_cents += amt // (count or 1)
                # day/week: ignore — we don't issue them

    # ---- Revenue (rolling windows) --------------------------------------
    # Use paid invoices as the revenue source — that's what actually hit
    # the bank. Stripe.Charge would also work but invoices line up with
    # how we model billing in the rest of the app.
    #
    # The live walk is bounded to the last 90 days via the created[gte]
    # filter so the page count stays small no matter how many lifetime
    # invoices exist. The lifetime number itself comes from
    # get_lifetime_revenue_cents() which is cached for 24h — without
    # that bound this loop would touch every paid invoice in account
    # history on every /admin/billing call.
    now = datetime.now().timestamp()
    day = 24 * 60 * 60
    windows = {"30d": int(now - 30 * day), "90d": int(now - 90 * day)}
    revenue_cents = {"30d": 0, "90d": 0}

    inv_iter = stripe.Invoice.list(
        limit=100,
        status="paid",
        created={"gte": windows["90d"]},
    )
    # auto_paging_iter walks past the first page transparently
    for inv in inv_iter.auto_paging_iter():
        amt = inv.amount_paid or 0
        if inv.created >= windows["30d"]:
            revenue_cents["30d"] += amt
        if inv.created >= windows["90d"]:
            revenue_cents["90d"] += amt

    # Lifetime number from the 24h TTL cache. Strictly an upper bound
    # on the live 90d window: even with cache lag, lifetime is always
    # at least last90dUsd (since the 90d figure is a subset of
    # lifetime). Take max(lifetime_cached, last90d_live) so the UI
    # never shows a smaller lifetime than the visible 90d figure.
    lifetime_cents_cached = get_lifetime_revenue_cents()
    lifetime_cents = max(lifetime_cents_cached, revenue_cents["90d"])

    # ---- Recent invoices (any status) -----------------------------------
    recent = stripe.Invoice.list(limit=20)
    invoices = []
    for inv in recent.data:
        invoices.append(
            {
                "id": inv.id,
                "customerId": inv.customer,
                "amountUsd": (inv.amount_due or 0) / 100.0,
                "amountPaidUsd": (inv.amount_paid or 0) / 100.0,
                "status": inv.status,
                "description": (inv.description or _first_line_item_desc(inv) or ""),
                "createdAt": datetime.fromtimestamp(inv.created).isoformat(),
                "hostedInvoiceUrl": inv.hosted_invoice_url,
            }
        )

    return {
        "subscriptions": {
            "active": counts["active"],
            "pastDue": counts["past_due"],
            "trialing": counts["trialing"],
            "canceled": counts["canceled"],
            "mrrUsd": mrr_cents / 100.0,
        },
        "revenue": {
            "last30dUsd": revenue_cents["30d"] / 100.0,
            "last90dUsd": revenue_cents["90d"] / 100.0,
            "lifetimeUsd": lifetime_cents / 100.0,
        },
        "invoices": invoices,
    }


def admin_stripe_account_snapshot() -> dict:
    """Account-level Stripe state for the StripeAccountBox.

    Distinct from ``admin_billing_snapshot`` which is about per-
    customer billing activity. This returns the *platform* view: are
    payouts going through, what's our balance, what's the bank on
    file, are there open disputes, etc. — the stuff a single solo
    operator needs to know to keep the merchant account healthy.

    All Stripe calls are cheap reads (no per-customer fan-out), but
    we still try/except each block so a single API hiccup doesn't
    blank the entire panel.
    """
    _configure()
    out: dict = {
        "configured": bool(os.environ.get("STRIPE_SECRET_KEY")),
        "errors": [],
    }
    # Initialized to empty dict so the external_account block below can
    # safely reference it even when Account.retrieve() fails (e.g., the
    # current Restricted API Key is missing rak_accounts_kyc_basic_read).
    a: dict = {}

    # ---- Account: capabilities + branding -----------------------------
    try:
        acct = stripe.Account.retrieve()
        a = acct.to_dict() if hasattr(acct, "to_dict") else dict(acct)
        bp = a.get("business_profile") or {}
        settings = a.get("settings") or {}
        branding = (settings.get("branding") or {}) if isinstance(settings, dict) else {}
        payouts = (settings.get("payouts") or {}) if isinstance(settings, dict) else {}
        sched = payouts.get("schedule") or {}
        out["account"] = {
            "id": a.get("id"),
            "country": a.get("country"),
            "defaultCurrency": a.get("default_currency"),
            "chargesEnabled": a.get("charges_enabled"),
            "payoutsEnabled": a.get("payouts_enabled"),
            "detailsSubmitted": a.get("details_submitted"),
            "businessName": bp.get("name"),
            "supportEmail": bp.get("support_email"),
            "supportPhone": bp.get("support_phone"),
            "supportUrl": bp.get("support_url"),
            "url": bp.get("url"),
            "hasLogo": bool(branding.get("logo")),
            "hasIcon": bool(branding.get("icon")),
            "primaryColor": branding.get("primary_color"),
            "payoutSchedule": (
                f"{sched.get('interval', '?')} "
                f"(delay {sched.get('delay_days', '?')}d)"
            ),
            "payoutStatementDescriptor": payouts.get("statement_descriptor"),
            "minimumBalanceUsd": (
                (payouts.get("minimum_balance") or {}).get("amount", 0) / 100.0
                if payouts.get("minimum_balance")
                else None
            ),
        }
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"account: {e}")
        out["account"] = None

    # ---- Balance -------------------------------------------------------
    try:
        bal = stripe.Balance.retrieve()
        b = bal.to_dict() if hasattr(bal, "to_dict") else dict(bal)
        avail = b.get("available") or []
        pending = b.get("pending") or []
        out["balance"] = {
            "available": [
                {"amountUsd": (x.get("amount") or 0) / 100.0, "currency": x.get("currency")}
                for x in avail
            ],
            "pending": [
                {"amountUsd": (x.get("amount") or 0) / 100.0, "currency": x.get("currency")}
                for x in pending
            ],
        }
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"balance: {e}")
        out["balance"] = None

    # ---- External (bank) account on file ------------------------------
    # Stripe's REST won't list external_accounts on a platform's own
    # account from the API key (it's a connect-only call), so we infer
    # by trying Account.retrieve()'s external_accounts field. Some
    # accounts surface it, some don't — handle both.
    try:
        ea = (a.get("external_accounts") or {}) if isinstance(a, dict) else {}
        items = ea.get("data") if isinstance(ea, dict) else None
        if items:
            first = items[0]
            out["externalAccount"] = {
                "bankName": first.get("bank_name"),
                "last4": first.get("last4"),
                "currency": first.get("currency"),
                "country": first.get("country"),
                "object": first.get("object"),  # 'bank_account' or 'card'
            }
        else:
            out["externalAccount"] = None
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"external_account: {e}")
        out["externalAccount"] = None

    # ---- Recent payouts -----------------------------------------------
    try:
        payouts_resp = stripe.Payout.list(limit=5)
        out["recentPayouts"] = [
            {
                "id": p.id,
                "amountUsd": (p.amount or 0) / 100.0,
                "currency": p.currency,
                "status": p.status,
                "arrivalDate": (
                    datetime.fromtimestamp(p.arrival_date).isoformat()
                    if p.arrival_date
                    else None
                ),
                "method": getattr(p, "method", None),
            }
            for p in payouts_resp.data
        ]
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"payouts: {e}")
        out["recentPayouts"] = []

    # ---- Open disputes count ------------------------------------------
    try:
        disputes_resp = stripe.Dispute.list(limit=100)
        open_count = 0
        for d in disputes_resp.data:
            if d.status in ("needs_response", "warning_needs_response", "under_review", "warning_under_review"):
                open_count += 1
        out["disputes"] = {"openCount": open_count}
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"disputes: {e}")
        out["disputes"] = {"openCount": None}

    return out


def admin_business_ops_costs(since_unix: int) -> dict:
    """Live business-ops costs for the P&L tab.

    Business-ops costs are the real-world expenses that aren't part of
    the product cost model (which lives in the Expenses tab and covers
    R2 storage + ops). Per the architecture set in May 2026:

      Expenses tab  = static per-user product cost model
      Business-ops  = Stripe fees, taxes, compliance, royalties,
                      affiliate program, customer service, etc.

    This function answers "how much of the business-ops bucket has
    actually been consumed".

    Categories returned:

      stripeFeesUsd   - sum of every Stripe BalanceTransaction with
                        type='stripe_fee' since the cutoff. Includes
                        processing fees on charges, dispute fees on
                        chargebacks (which appear as type='stripe_fee'
                        with description containing 'Dispute'), and
                        any other fee Stripe deducts.

      taxesUsd        - $0 placeholder. Activates when we register
                        with a tax authority and start collecting +
                        remitting. Lives in this function so the slot
                        exists in the UI from day one.

      royaltiesUsd    - $0 placeholder. Activates when investor
                        royalties are set up.

      affiliateUsd    - $0 placeholder. Activates when the affiliate
                        program launches.

      customerSupportUsd - $0 placeholder. Activates when we hire
                           or contract for support.

      totalUsd        - sum of all of the above.

    Each placeholder field has a corresponding 'note' string so the
    UI can render WHY it's $0 today.
    """
    _configure()
    out: dict = {
        "stripeFeesUsd": 0.0,
        "taxesUsd": 0.0,
        "royaltiesUsd": 0.0,
        "affiliateUsd": 0.0,
        "customerSupportUsd": 0.0,
        "totalUsd": 0.0,
        "notes": {
            "stripeFees": (
                "Live from Stripe BalanceTransaction API (type='stripe_fee') "
                "since the cutoff. Includes processing fees + dispute fees."
            ),
            "taxes": "Not collected yet. Activates when we register with a tax authority.",
            "royalties": "Not set up. Activates when investor agreements are in place.",
            "affiliate": "Not launched. Activates when the affiliate program goes live.",
            "customerSupport": "Not contracted. Activates when we hire or use a support vendor.",
        },
        "errors": [],
    }

    # Walk BalanceTransaction with auto_paging since fees accumulate
    # one row per transaction - even at low scale we might cross the
    # 100-row default limit over a 30-day window.
    try:
        cents = 0
        txns = stripe.BalanceTransaction.list(
            type="stripe_fee", limit=100, created={"gte": since_unix}
        )
        for t in txns.auto_paging_iter():
            # fee_amount on a stripe_fee transaction equals the
            # transaction amount itself (Stripe records the fee as a
            # negative balance movement). Use abs(amount) to get the
            # dollar magnitude of the fee.
            amt = t.amount or 0
            cents += abs(amt)
        out["stripeFeesUsd"] = round(cents / 100.0, 4)
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"stripe_fees: {e}")

    out["totalUsd"] = round(
        out["stripeFeesUsd"]
        + out["taxesUsd"]
        + out["royaltiesUsd"]
        + out["affiliateUsd"]
        + out["customerSupportUsd"],
        4,
    )
    return out


def _first_line_item_desc(inv) -> Optional[str]:
    """Best-effort fallback: pull the first line-item description so
    the Billing tab has something to show for invoices that didn't get
    their top-level `description` set (storage invoices in particular).
    """
    try:
        lines = inv.lines.data if inv.lines and inv.lines.data else []
        if lines:
            return lines[0].description
    except Exception:  # noqa: BLE001
        pass
    return None


def compute_final_charge(unbilled_usd: float) -> dict:
    """Given a user's unbilled storage in dollars, compute the final
    charge breakdown for the account-deletion flow.

    Rule: amounts below MIN_INVOICE_USD (currently $5) get a flat
    SMALL_CHARGE_FEE_USD ($0.55) added on top to cover Stripe's
    per-transaction fee, since on tiny amounts the fee can exceed
    what's owed. Amounts at or above the threshold pay no fee - the
    Stripe cut is small in absolute terms there and absorbing it
    keeps the final invoice friendly.

    Returns a dict with 'storage_usd', 'fee_usd', 'total_usd' so the
    caller can show the breakdown in the UI before charging.
    """
    storage_usd = max(0.0, unbilled_usd)
    if storage_usd <= 0:
        return {"storage_usd": 0.0, "fee_usd": 0.0, "total_usd": 0.0}
    if storage_usd < MIN_INVOICE_USD:
        fee_usd = SMALL_CHARGE_FEE_USD
    else:
        fee_usd = 0.0
    return {
        "storage_usd": round(storage_usd, 4),
        "fee_usd": round(fee_usd, 2),
        "total_usd": round(storage_usd + fee_usd, 4),
    }


def storage_period_description(start: datetime, end: datetime) -> str:
    """Human-readable label for a storage line item, e.g. 'Storage — May 2026'.

    Single-month → 'Storage — May 2026'.
    Same year, multi-month (carry-over) → 'Storage — Apr–May 2026'.
    Cross-year → 'Storage — Dec 2025–Jan 2026'.
    """
    if start.year == end.year and start.month == end.month:
        return f"Storage — {start.strftime('%B %Y')}"
    if start.year == end.year:
        return f"Storage — {start.strftime('%b')}–{end.strftime('%b')} {end.year}"
    return f"Storage — {start.strftime('%b %Y')}–{end.strftime('%b %Y')}"


def ops_period_description(
    start: datetime, end: datetime, class_a: int, class_b: int
) -> str:
    """Human-readable label for an R2 operations line item.

    Shape: 'R2 operations — May 2026 (1.2M Class A · 4.5M Class B)'.
    The counts are folded in so the invoice line tells the user
    exactly what they're paying for; matches the philosophy from
    the storage line where the period is in the description.
    """

    def _abbrev(n: int) -> str:
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}K"
        return str(n)

    if start.year == end.year and start.month == end.month:
        period = start.strftime("%B %Y")
    elif start.year == end.year:
        period = f"{start.strftime('%b')}–{end.strftime('%b')} {end.year}"
    else:
        period = f"{start.strftime('%b %Y')}–{end.strftime('%b %Y')}"
    return (
        f"R2 operations — {period} "
        f"({_abbrev(class_a)} Class A · {_abbrev(class_b)} Class B)"
    )
