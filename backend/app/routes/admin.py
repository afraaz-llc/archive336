"""Admin-only endpoints — Phase A.

What's here:
  - GET /api/admin/system  : aggregate counts (users, archived bytes,
                             unbilled accruals, recent payment events).
  - GET /api/admin/users   : paginated list of every user with payment
                             status, storage usage, last login.
  - GET /api/admin/stack   : returns the contents of /STACK.md so the
                             admin UI can render the canonical service
                             list without duplicating it.

All endpoints are gated by Depends(get_admin_user) — non-admin sessions
get 403. Admin status is set via SQL, not via this API. See
`get_admin_user` docstring in app.security for why.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app import billing as billing_lib
from app import r2
from app.db import get_db
from app.models import (
    EmailSendLog,
    ErrorLog,
    ReconciliationLog,
    StorageObject,
    StripeAuditLog,
    UsageRecord,
    User,
    UserChannel,
    UserChannelVideo,
    UserSession,
)
from app.security import get_admin_user


log = logging.getLogger("archive336.admin")

router = APIRouter()


# ---------- /system : aggregate counts ----------


@router.get("/system")
def system_metrics(
    db: Session = Depends(get_db),
    _admin: User = Depends(get_admin_user),
) -> Dict[str, Any]:
    """Top-level metrics for the admin home tab."""
    user_count = db.query(User).count()
    verified_count = db.query(User).filter(User.email_verified.is_(True)).count()
    paying_count = (
        db.query(User).filter(User.payment_status == "active").count()
    )
    past_due_count = (
        db.query(User).filter(User.payment_status == "past_due").count()
    )

    # Sum of archived bytes from every user's video rows. The metering
    # cron also computes this daily but we walk fresh here so the panel
    # reflects right now, not last 02:00 UTC.
    total_bytes = 0
    for r in db.query(UserChannelVideo).all():
        try:
            data = json.loads(r.data_json)
            n = data.get("fileSizeBytes")
            if isinstance(n, int) and n > 0:
                total_bytes += n
        except json.JSONDecodeError:
            continue

    # Unbilled storage value across all users. Useful to know how much
    # is queued for the next 3rd-of-month run.
    unbilled_records = (
        db.query(UsageRecord).filter(UsageRecord.billed.is_(False)).all()
    )
    total_unbilled_gb_days = sum(
        r.bytes_stored / billing_lib.BYTES_PER_GB for r in unbilled_records
    )
    total_unbilled_usd = billing_lib.gb_days_to_usd(total_unbilled_gb_days)

    # Recent signups: last 7 days
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    recent_signups = (
        db.query(User).filter(User.created_at >= week_ago).count()
    )

    # Resend sends. Count rows in our own EmailSendLog so we don't
    # need a higher-scope Resend API key just to surface a quota number.
    # Free tier: 3,000/mo, 100/day.
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    sends_today = (
        db.query(EmailSendLog)
        .filter(EmailSendLog.created_at >= today_start)
        .count()
    )
    sends_this_month = (
        db.query(EmailSendLog)
        .filter(EmailSendLog.created_at >= month_start)
        .count()
    )

    # R2 actually-stored bytes (separate from what our DB believes).
    # Drift means orphan files (R2 > DB, costs us money) or broken
    # uploads (DB > R2, user paid for nothing).
    r2_objects: Optional[int] = None
    r2_bytes: Optional[int] = None
    r2_cost_usd: Optional[float] = None
    try:
        from app import ops_ledger
        stats = r2.bucket_stats(subject=ops_ledger.PLATFORM)
        if stats is not None:
            r2_objects = stats["objects"]
            r2_bytes = stats["bytes"]
            # R2 standard storage at Cloudflare's published rate — this
            # is the wholesale cost line, not revenue (we mark it up 2×
            # before billing the user). Source of truth in billing.py.
            r2_cost_usd = round(
                (r2_bytes / billing_lib.BYTES_PER_GB)
                * billing_lib.STORAGE_COST_PER_GB_PER_MONTH_USD,
                4,
            )
    except Exception:  # noqa: BLE001
        log.warning("R2 bucket_stats() failed", exc_info=True)

    return {
        "users": {
            "total": user_count,
            "emailVerified": verified_count,
            "paying": paying_count,
            "pastDue": past_due_count,
            "recentSignups": recent_signups,
        },
        "storage": {
            "totalBytes": total_bytes,
            "totalGb": round(total_bytes / 1_000_000_000.0, 3),
        },
        "r2": {
            "objects": r2_objects,
            "bytes": r2_bytes,
            "monthlyCostUsd": r2_cost_usd,
        },
        "resend": {
            "sendsToday": sends_today,
            "sendsThisMonth": sends_this_month,
            "freeMonthlyLimit": 3000,
            "freeDailyLimit": 100,
        },
        "billing": {
            "unbilledGbDays": round(total_unbilled_gb_days, 2),
            "unbilledUsd": round(total_unbilled_usd, 4),
        },
    }


# ---------- /users : paginated list ----------


@router.get("/users")
def list_users(
    db: Session = Depends(get_db),
    _admin: User = Depends(get_admin_user),
    q: Optional[str] = Query(default=None, description="Username/email substring"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> Dict[str, Any]:
    """List users with computed storage usage. Optional search by
    username or email substring (case-insensitive)."""
    query = db.query(User)
    if q:
        like = f"%{q.lower()}%"
        query = query.filter(
            (User.username.ilike(like)) | (User.email.ilike(like))
        )
    total = query.count()
    rows = query.order_by(User.created_at.desc()).offset(offset).limit(limit).all()

    # Compute storage per user from the video rows. fileSizeBytes
    # lives inside the JSON blob in UserChannelVideo.data_json - it
    # isn't a SQL column - so the sum uses SQLite's json_extract()
    # (JSON1 extension, built into SQLite 3.38+ which the deploy
    # box has). The previous Python-side loop pulled every row for
    # every user on the page into the app process just to add up
    # numbers; at 1k users × 5k videos avg that was 250k rows per
    # request. The aggregate keeps the DB on the hook and ships just
    # one row per user.
    #
    # The CASE WHEN json_valid(...) wrap matches the old loop's
    # silent-skip-on-bad-JSON behavior: json_extract raises an
    # OperationalError on malformed JSON which would 500 the whole
    # response. COALESCE then turns NULL extractions (missing key,
    # null value, bad JSON) into 0, and CAST coerces JSON
    # strings/floats into integers (the old loop required isinstance
    # int and skipped strings/floats — this is a small drift but in
    # practice fileSizeBytes is always an integer set by the worker
    # so the difference is theoretical).
    from sqlalchemy import case as _sql_case
    from sqlalchemy import func
    from sqlalchemy import Integer as _SQLInteger
    user_ids = [u.id for u in rows]
    by_user: Dict[str, int] = {}
    if user_ids:
        size_expr = func.coalesce(
            _sql_case(
                (
                    func.json_valid(UserChannelVideo.data_json) == 1,
                    func.json_extract(
                        UserChannelVideo.data_json, "$.fileSizeBytes"
                    ),
                ),
                else_=None,
            ),
            0,
        )
        for uid, total_bytes in (
            db.query(
                UserChannelVideo.user_id,
                func.sum(size_expr.cast(_SQLInteger)),
            )
            .filter(UserChannelVideo.user_id.in_(user_ids))
            .group_by(UserChannelVideo.user_id)
            .all()
        ):
            by_user[uid] = int(total_bytes or 0)

    # Last session time per user (proxy for "last seen")
    last_seen: Dict[str, datetime] = {}
    for s in (
        db.query(UserSession)
        .filter(UserSession.user_id.in_(user_ids))
        .all()
    ):
        prev = last_seen.get(s.user_id)
        if prev is None or s.created_at > prev:
            last_seen[s.user_id] = s.created_at

    # R2 ops per user this calendar month so far (subject == user_id;
    # platform ops are excluded by definition). Phase E of the R2 ops
    # billing redesign. We pull this as a single grouped query for the
    # whole page rather than N queries per user — cheap and bounded.
    from app.models import R2OperationLog
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    ops_by_user: Dict[str, Dict[str, int]] = {uid: {"A": 0, "B": 0} for uid in user_ids}
    for subj, cls, total_count in (
        db.query(
            R2OperationLog.subject,
            R2OperationLog.op_class,
            func.sum(R2OperationLog.count),
        )
        .filter(
            R2OperationLog.subject.in_(user_ids),
            R2OperationLog.day >= month_start,
        )
        .group_by(R2OperationLog.subject, R2OperationLog.op_class)
        .all()
    ):
        if subj in ops_by_user and cls in ops_by_user[subj]:
            ops_by_user[subj][cls] = int(total_count or 0)

    out: List[Dict[str, Any]] = []
    for u in rows:
        ls = last_seen.get(u.id)
        ops = ops_by_user.get(u.id, {"A": 0, "B": 0})
        out.append(
            {
                "id": u.id,
                "username": u.username,
                "email": u.email,
                "emailVerified": u.email_verified,
                "isAdmin": u.is_admin,
                "paymentStatus": u.payment_status,
                "stripeCustomerId": u.stripe_customer_id,
                "createdAt": u.created_at.isoformat(),
                "lastSeenAt": ls.isoformat() if ls else None,
                "storageBytes": by_user.get(u.id, 0),
                # R2 ops since the start of this UTC calendar month.
                # Class A = writes/lists, Class B = reads. Multiply by
                # billing.R2_CLASS_A/B_USD_PER_MILLION × 2x markup to
                # get the user's accrued ops charge for the period.
                "opsMonthA": ops["A"],
                "opsMonthB": ops["B"],
            }
        )

    return {"total": total, "limit": limit, "offset": offset, "items": out}


# ---------- /billing : Stripe revenue snapshot ----------


@router.get("/billing")
def billing_snapshot(
    db: Session = Depends(get_db),
    _admin: User = Depends(get_admin_user),
) -> Dict[str, Any]:
    """Live snapshot of Stripe-side billing state for the admin Billing tab.

    Joins the Stripe data with our user table so each invoice line shows
    the ARCHIVE336 username/email instead of a bare cus_xxx id. Stripe is
    the source of truth here; the DB is just a lookup for display.
    """
    membership_price_id = os.environ.get("STRIPE_MEMBERSHIP_PRICE_ID", "")
    if not membership_price_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="STRIPE_MEMBERSHIP_PRICE_ID not configured.",
        )

    snap = billing_lib.admin_billing_snapshot(membership_price_id)

    # Resolve customer ids → user labels in one DB pass.
    cust_ids = {inv["customerId"] for inv in snap["invoices"] if inv["customerId"]}
    label_by_cust: Dict[str, Dict[str, str]] = {}
    if cust_ids:
        for u in (
            db.query(User)
            .filter(User.stripe_customer_id.in_(list(cust_ids)))
            .all()
        ):
            label_by_cust[u.stripe_customer_id] = {
                "username": u.username,
                "email": u.email,
            }
    for inv in snap["invoices"]:
        meta = label_by_cust.get(inv["customerId"]) if inv["customerId"] else None
        inv["username"] = meta["username"] if meta else None
        inv["email"] = meta["email"] if meta else None

    return snap


# ---------- /stripe-account : platform-level Stripe health ----------


@router.get("/stripe-account")
def stripe_account(
    _admin: User = Depends(get_admin_user),
) -> Dict[str, Any]:
    """Account-level Stripe state for the StripeAccountBox.

    Returns capabilities (charges_enabled, payouts_enabled), branding
    config, payout schedule, current balance, bank account on file,
    recent payouts, open dispute count. See
    ``billing.admin_stripe_account_snapshot`` for the full shape.
    """
    try:
        return billing_lib.admin_stripe_account_snapshot()
    except Exception:
        log.warning("admin stripe-account snapshot failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Couldn't reach Stripe for the account snapshot.",
        )


# ---------- /identifiers : account ids, admin-only ----------


@router.get("/identifiers")
def admin_identifiers(
    _admin: User = Depends(get_admin_user),
) -> Dict[str, Any]:
    """Provider account ids and the origin IP, for the admin panel.

    These used to be hardcoded constants in Admin.tsx. That file is
    lazy-loaded, which is not access control: the chunk is a static
    asset and fetches with HTTP 200 for anyone who asks, so the origin
    IP, the Mercury account id, and the Stripe and Cloudflare account
    ids were being served to every visitor.

    The origin IP mattered most. The site is behind Cloudflare's proxy,
    which only protects an origin nobody can address directly - so
    publishing it in the bundle handed anyone a way around the CDN and
    the WAF.

    Read from env so nothing identifying lives in the repo either.
    """
    return {
        "originIpv4": os.environ.get("ORIGIN_IPV4", ""),
        # Same exposure class as the IPv4 - a routable address for the
        # origin, which is the one thing the proxy in front of it
        # cannot protect once it is public.
        "originIpv6": os.environ.get("ORIGIN_IPV6", ""),
        "sshKeyPath": os.environ.get("SSH_KEY_PATH", ""),
        "ownerEmail": os.environ.get("ADMIN_ALERT_EMAIL", ""),
        # The alias the provider consoles are logged in under. Was
        # hardcoded five times in Admin.tsx and reached the bundle with
        # everything else.
        "providerLogin": os.environ.get("PROVIDER_LOGIN_EMAIL", ""),
        # Cloudflare predates the rename and is still registered under
        # the old alias - a separate value, not the same login.
        "cloudflareLogin": os.environ.get("CLOUDFLARE_LOGIN_EMAIL", ""),
        "mercuryAccountId": os.environ.get("MERCURY_ACCOUNT_ID", ""),
        "stripeAccountId": os.environ.get("STRIPE_ACCOUNT_ID", ""),
        "cloudflareAccountId": os.environ.get(
            "CLOUDFLARE_ACCOUNT_ID", os.environ.get("R2_ACCOUNT_ID", "")
        ),
    }


# ---------- /mercury-account : Mercury Bank live snapshot ----------


@router.get("/mercury-account")
def mercury_account(
    _admin: User = Depends(get_admin_user),
) -> Dict[str, Any]:
    """Live Mercury Bank account state for the MercuryAccountBox.

    Returns balance, recent transactions, and a ``configured`` flag so
    the frontend can render a "set MERCURY_API_KEY" hint when the key
    is missing instead of failing the request. The full account number
    is dropped server-side; only the last 4 reaches the browser.
    See ``app.mercury.admin_mercury_snapshot`` for the full response
    shape.
    """
    from app import mercury as mercury_lib
    try:
        return mercury_lib.admin_mercury_snapshot()
    except Exception:
        log.warning("admin mercury-account snapshot failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Couldn't reach Mercury for the account snapshot.",
        )


# ---------- /stripe-audit-log : webhook event feed ----------


@router.get("/stripe-audit-log")
def stripe_audit_log_feed(
    limit: int = Query(50, ge=1, le=500),
    event_type: Optional[str] = Query(
        None,
        description="Filter to a specific event_type (e.g. 'payout.failed').",
    ),
    db: Session = Depends(get_db),
    _admin: User = Depends(get_admin_user),
) -> Dict[str, Any]:
    """Recent Stripe webhook events from our audit log.

    Joined with users so each row carries username/email when the
    event was for a known customer. Most recent first.
    """
    q = db.query(StripeAuditLog).order_by(StripeAuditLog.received_at.desc())
    if event_type:
        q = q.filter(StripeAuditLog.event_type == event_type)
    rows = q.limit(limit).all()

    user_ids = {r.user_id for r in rows if r.user_id}
    label_by_uid: Dict[str, Dict[str, str]] = {}
    if user_ids:
        for u in db.query(User).filter(User.id.in_(list(user_ids))).all():
            label_by_uid[u.id] = {"username": u.username, "email": u.email}

    # Per-event-type totals over the last 24h. Useful summary for the UI.
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    from sqlalchemy import func as _func
    by_type_24h_rows = (
        db.query(
            StripeAuditLog.event_type,
            _func.count(StripeAuditLog.id),
        )
        .filter(StripeAuditLog.received_at >= cutoff)
        .group_by(StripeAuditLog.event_type)
        .all()
    )
    by_type_24h = {t: c for (t, c) in by_type_24h_rows}

    events = []
    for r in rows:
        meta = label_by_uid.get(r.user_id) if r.user_id else None
        events.append(
            {
                "id": r.id,
                "stripeEventId": r.stripe_event_id,
                "eventType": r.event_type,
                "receivedAt": r.received_at.isoformat() if r.received_at else None,
                "stripeCustomerId": r.stripe_customer_id,
                "username": meta["username"] if meta else None,
                "email": meta["email"] if meta else None,
                "handled": r.handled,
                "notes": r.notes,
            }
        )

    return {
        "events": events,
        "byTypeLast24h": by_type_24h,
        "totalShown": len(events),
    }


# ---------- /pnl : monthly cost + revenue roll-up ----------


@router.get("/pnl")
def pnl(
    db: Session = Depends(get_db),
    _admin: User = Depends(get_admin_user),
) -> Dict[str, Any]:
    """Monthly P&L snapshot: every cost line we know about vs.
    every revenue line, plus net for the trailing 30 days.

    Costs are the source of truth that STACK.md describes; if those
    rates change in real life, update them here too. Revenue is pulled
    from Stripe live (no DB cache) so the number is always today's
    truth, not 'as of last bill run'.
    """
    # ---- Fixed monthly costs ----
    # Sourced from the canonical _PLATFORM_ITEMS list (the Expenses tab's
    # data source) so the two surfaces never drift. The previous
    # hardcoded $5/mo for Hetzner was stale - the real cost is
    # $107.88/yr ($8.99/mo) Hetzner CPX11 + IPv4 + Backups, plus the
    # $1/yr Litestream R2 backups bucket the old version missed.
    def _item_annual(name: str) -> float:
        for it in _PLATFORM_ITEMS:
            if it["name"] == name and it["state"] == "active":
                return float(it["annualUsd"])
        return 0.0

    hetzner_usd = round(_item_annual("Hetzner CPX11 + IPv4 + Backups") / 12, 4)
    litestream_amortized_usd = round(
        _item_annual("Litestream R2 (backups bucket)") / 12, 4
    )
    fixed_usd = round(
        hetzner_usd + litestream_amortized_usd, 4
    )

    # ---- Variable: R2 storage ----
    # Walked live from the bucket so we never let DB drift mask the
    # real bill (the System tab also flags drift independently).
    r2_cost_usd = 0.0
    try:
        from app import ops_ledger
        stats = r2.bucket_stats(subject=ops_ledger.PLATFORM)
        if stats is not None:
            r2_cost_usd = round(
                (stats["bytes"] / billing_lib.BYTES_PER_GB)
                * billing_lib.STORAGE_COST_PER_GB_PER_MONTH_USD,
                4,
            )
    except Exception:  # noqa: BLE001
        log.warning("R2 bucket_stats() failed in /pnl", exc_info=True)

    # ---- Variable: Resend tier ----
    # Free under 3,000 sends/mo; the next tier (Pro) is $20/mo. We
    # snap to one or the other - no proration since Resend bills
    # whole-tier-up at first overage.
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    sends_this_month = (
        db.query(EmailSendLog)
        .filter(EmailSendLog.created_at >= month_start)
        .count()
    )
    resend_usd = 0.0 if sends_this_month <= 3000 else 20.0

    total_cost_usd = round(fixed_usd + r2_cost_usd + resend_usd, 4)

    # ---- Revenue: live from Stripe ----
    membership_price_id = os.environ.get("STRIPE_MEMBERSHIP_PRICE_ID", "")
    mrr_usd = 0.0
    last_30d_revenue_usd = 0.0
    if membership_price_id:
        try:
            snap = billing_lib.admin_billing_snapshot(membership_price_id)
            mrr_usd = snap["subscriptions"]["mrrUsd"]
            last_30d_revenue_usd = snap["revenue"]["last30dUsd"]
        except Exception:  # noqa: BLE001
            log.warning("Stripe snapshot failed in /pnl", exc_info=True)

    # ---- Business-ops costs: real-world expenses, NOT product cost ----
    # Stripe fees, taxes, royalties, affiliate, customer support. The
    # Expenses tab cost model intentionally excludes these - it models
    # the product only. P&L surfaces what has actually been consumed.
    cutoff_unix = int((now - timedelta(days=30)).timestamp())
    business_ops: Dict[str, Any] = {}
    try:
        business_ops = billing_lib.admin_business_ops_costs(cutoff_unix)
    except Exception:
        log.warning("business_ops snapshot failed in /pnl", exc_info=True)
        business_ops = {
            "stripeFeesUsd": 0.0,
            "taxesUsd": 0.0,
            "royaltiesUsd": 0.0,
            "affiliateUsd": 0.0,
            "customerSupportUsd": 0.0,
            "totalUsd": 0.0,
            "notes": {},
            "errors": ["business_ops snapshot failed; see server logs"],
        }
    business_ops_total = business_ops.get("totalUsd", 0.0)

    # Net = revenue collected over the last 30 days minus everything
    # we paid for the same period. Both product costs (Hetzner, R2,
    # etc.) AND business-ops costs (Stripe fees, etc.) are subtracted
    # so the bottom line is the actual margin available for everything
    # the product margin is supposed to cover.
    net_last_30d_usd = round(
        last_30d_revenue_usd - total_cost_usd - business_ops_total, 4
    )

    return {
        "costs": {
            "hetznerUsd": round(hetzner_usd, 4),
            "litestreamAmortizedUsd": round(litestream_amortized_usd, 4),
            "fixedUsd": round(fixed_usd, 4),
            "r2Usd": r2_cost_usd,
            "resendUsd": resend_usd,
            "totalUsd": total_cost_usd,
        },
        "businessOps": business_ops,
        "revenue": {
            "mrrUsd": round(mrr_usd, 4),
            "last30dUsd": round(last_30d_revenue_usd, 4),
        },
        "netLast30dUsd": net_last_30d_usd,
    }


# ---------- /expenses : static three-bucket cost model document ----------


# This endpoint is the canonical "what this business is being charged
# for or could be charged for" document. Pure static structure — no
# DB queries, no live measurement. The companion /live endpoint is
# where live MTD numbers live.
#
# Line-item lifecycle states:
#   "active" — paying real $ for this today
#   "latent" — $0 today but already in the architecture; activates at
#              a known threshold as we grow (no code change needed)
#
# By design there is no "speculative" state — the audit is about the
# real cost picture, not hypothetical future features. If a service
# isn't in the architecture today, it doesn't earn a line item until
# it is.
#
# When a cost changes in real life (Hetzner re-prices, we cross a
# Sentry tier, etc.) update the values below and redeploy. Single
# source of truth — the AccountBoxes all reference these same numbers.

_PLATFORM_ITEMS = [
    {
        "name": "Hetzner CPX11 + IPv4 + Backups",
        "state": "active",
        "annualUsd": 107.88,
        "note": "$6.99 base + $0.60 IPv4 + $1.40 backups, monthly. Ashburn region. Fixed regardless of users.",
    },
]

# Metered (per-usage) cost lines for the Expenses tab.
#
# COMPUTED from backend/app/billing.py rather than typed in. The previous
# version was a block of hardcoded strings quoting Cloudflare R2's rates
# (quoting Cloudflare R2's old rates) written
# before the move to Backblaze and before the 2026-06-04 re-pricing. Every
# figure in it was wrong, on the one screen whose entire purpose is
# reasoning about unit economics - so the numbers you would use to make a
# pricing decision were the numbers nobody had updated. Deriving them
# means they cannot go stale again.
def _metered_items() -> List[Dict[str, Any]]:
    cost = billing_lib.STORAGE_COST_PER_GB_PER_MONTH_USD
    basic = billing_lib.STORAGE_PRICE_PER_GB_MONTH_BY_TIER["basic"]
    creator = billing_lib.STORAGE_PRICE_PER_GB_MONTH_BY_TIER["creator"]
    studio = billing_lib.STORAGE_PRICE_PER_GB_MONTH_BY_TIER["studio"]
    margin_pct = round((1 - cost / basic) * 100) if basic else None
    return [
        {
            "name": "Backblaze B2 storage byte-hours",
            "state": "profitable",
            "ourCost": f"${cost:.4f} / GB-month",
            "billedAtMarkup": (
                f"${basic:.3f} / GB-month Basic ({basic / cost:.1f}x)"
                if cost
                else "n/a"
            ),
            "marginPct": margin_pct,
            "note": (
                "Per-user byte-hour ledger (StorageObject table). Smallest "
                "billable unit: 1 byte stored for 1 hour. Per-GB-month rate "
                f"by tier: Basic ${basic:.3f}, Creator ${creator:.3f}, "
                f"Studio ${studio:.4f}, against our ${cost:.4f} cost. "
                f"Rates derive "
                f"from decimal GB ({billing_lib.BYTES_PER_GB:,} bytes) and an "
                f"average month of {billing_lib.HOURS_PER_MONTH_AVG} hours "
                "(365.25 x 24 / 12)."
            ),
        },
        {
            "name": "Backblaze transactions (Class A/B/C)",
            "state": "absorbed",
            "ourCost": "$0.00 - free on B2",
            "billedAtMarkup": "not billed",
            "marginPct": None,
            "note": (
                "Uploads (Class A), downloads and HEADs (Class B) and LISTs "
                "(Class C) are all free on Backblaze pay-as-you-go. Every "
                "call this app makes is one of those. Only Class D is "
                "chargeable ($0.004 per 10,000, first 2,500/day free) and "
                "nothing here issues one. These lines previously quoted "
                "Cloudflare R2's $4.50 and $0.36 per million, which is what "
                "we would have paid on the provider we left."
            ),
        },
        {
            "name": "Egress (downloads to users)",
            "state": "absorbed",
            "ourCost": "$0.00 via Cloudflare",
            "billedAtMarkup": (
                f"${billing_lib.DOWNLOAD_PRICE_PER_GB_USD:.2f} / GB"
            ),
            "marginPct": None,
            "note": (
                "Large downloads stream B2 -> Cloudflare Worker "
                "(dl.archive336.com) -> user. B2->Cloudflare is free under "
                "the Bandwidth Alliance, so we bill users nothing. Direct "
                "(un-proxied) fetches - thumbnails, avatars - count against "
                "B2's free allowance of 3x average monthly storage, then "
                "cost $0.01/GB. Small today; watch it if thumbnail traffic "
                "ever dwarfs stored bytes."
            ),
        },
    ]


# NOTE on intentionally-excluded cost vectors:
# Stripe transaction fees, dispute fees, currency conversion, sales
# tax/VAT, royalties, affiliate payouts, compliance + legal in other
# countries, customer service hires, insurance — these are REAL costs
# and they DO come out of revenue, but they live in the
# business-operations bucket, not the product-cost bucket. The Expenses
# tab models the product (deliver storage + ops). The business-ops
# bucket is a separate concern with its own tracking surface, and is
# deliberately NOT internalized into per-user product math. Don't add
# Stripe
# fees here.


@router.get("/expenses")
def expenses(
    _admin: User = Depends(get_admin_user),
) -> Dict[str, Any]:
    """Static expense model document. Three buckets cover every expense
    vector this business is being charged for or could be charged for
    at known thresholds.

    Pure hardcoded structure — no live measurement happens here. For
    moment-to-moment cost numbers, see /api/admin/live. For the matching
    revenue document (what flows in vs. what flows out), see
    /api/admin/revenue.

    See module-level _PLATFORM_ITEMS / _metered_items() for
    the source of truth. Updated when real-world pricing changes
    (Hetzner re-prices, we cross a free tier, etc.).
    """
    platform_active_total = round(
        sum(i["annualUsd"] for i in _PLATFORM_ITEMS if i["state"] == "active"),
        2,
    )
    return {
        "platform": {
            "subtitle": "",
            "annualActiveUsd": platform_active_total,
            "items": _PLATFORM_ITEMS,
        },
        "metered": {
            "subtitle": "Variable per user. Charged at 2× our cost (free tier ignored — that's platform profit). Power users pay more; light users pay less.",
            "markupMultiplier": billing_lib.MARKUP,
            "items": _metered_items(),
        },
    }


# ---------- /revenue : static revenue model document ----------


# The inflow-side sibling of /expenses. Three buckets organized by
# business model (what the charge IS to the customer), with per-item
# fields exposing the billing mechanism (HOW it fires through Stripe)
# so the same document serves both pricing/strategy thinking AND
# code-debugging without having to switch tabs.
#
# Item schema:
#   name       — display name
#   state      — "active" | "latent" (latent = in the architecture,
#                will activate at a known threshold/product decision)
#   rate       — display string with the unit price
#   mechanism  — how the charge fires through Stripe:
#                "Stripe Subscription" (auto-renews),
#                "Stripe Invoice" (one-off invoice generated by us),
#                "Stripe InvoiceItem" (line added to another invoice)
#   trigger    — when the charge fires (events, cron, anniversary, etc.)
#   codeRef    — file:function in the backend where the charge originates
#   note       — free-text policy/context
#
# Each bucket also returns a `liveSummary` with the most relevant
# operational number(s), computed at endpoint time from existing
# sources (Stripe API + local DB). The static doc + live anchor =
# one place to look when thinking about a revenue line.

# Helper: format a money amount for display. Sub-dollar values are
# shown in cents (so "$0.329" becomes "¢32.9"), which is more
# readable than 3-decimal dollar strings; ≥$1 values stay in dollars.
# Cents symbol is placed on the LEFT (visually consistent with the
# dollar prefix) even though it's not the standard cents convention —
# the user prefers the column alignment over strict typography.
# Trailing zeros stripped in both forms.
def _fmt_money(usd: float) -> str:
    if usd != 0 and abs(usd) < 1.0:
        cents = usd * 100
        s = f"{cents:.2f}".rstrip("0").rstrip(".")
        return f"{s}¢"
    s = f"{usd:.4f}".rstrip("0").rstrip(".")
    return f"${s}"


def _net_by_method(gross_usd: float) -> dict:
    """Return per-scenario net strings for a given gross amount, via
    the canonical billing.stripe_net_* / stripe_loss_* helpers.

    Six scenarios covered — four "good path" payment methods (US card,
    international card USD, international card non-USD, ACH Direct
    Debit) plus two outcome scenarios (refund and chargeback). Every
    item in the revenue model carries this same shape so the table
    can render all possibilities in one pass — completeness over
    likelihood, even when some columns end up "n/a" or context-
    dependent."""
    return {
        "usCard": _fmt_money(billing_lib.stripe_net_us_card(gross_usd)),
        "intlCardUsd": _fmt_money(billing_lib.stripe_net_intl_card_usd(gross_usd)),
        "intlCardNonUsd": _fmt_money(
            billing_lib.stripe_net_intl_card_non_usd(gross_usd)
        ),
        "ach": _fmt_money(billing_lib.stripe_net_ach(gross_usd)),
        "refund": _fmt_money(billing_lib.stripe_loss_refund(gross_usd)),
        "chargeback": _fmt_money(billing_lib.stripe_loss_chargeback(gross_usd)),
    }


def _fee_by_method(gross_usd: float) -> dict:
    """Per-scenario fee AMOUNT (what Stripe takes), parallel to
    _net_by_method which returns what's left. fee = gross - net for
    payment methods; for refund/chargeback the "fee" is the total
    loss including the original processing fee and the dispute fee
    where applicable.

    Lets the Revenue tab show the formula AND the computed amount
    side by side per line item — readers don't have to mentally
    apply 2.9% + $0.30 to every gross figure to see what Stripe is
    taking from this specific line."""
    us_net = billing_lib.stripe_net_us_card(gross_usd)
    intl_usd_net = billing_lib.stripe_net_intl_card_usd(gross_usd)
    intl_non_usd_net = billing_lib.stripe_net_intl_card_non_usd(gross_usd)
    ach_net = billing_lib.stripe_net_ach(gross_usd)
    refund_net = billing_lib.stripe_loss_refund(gross_usd)
    chargeback_net = billing_lib.stripe_loss_chargeback(gross_usd)
    return {
        "usCard": _fmt_money(gross_usd - us_net),
        "intlCardUsd": _fmt_money(gross_usd - intl_usd_net),
        "intlCardNonUsd": _fmt_money(gross_usd - intl_non_usd_net),
        "ach": _fmt_money(gross_usd - ach_net),
        # Refund/chargeback "fee" = total loss to us = -net (since net
        # is already negative in those scenarios).
        "refund": _fmt_money(-refund_net),
        "chargeback": _fmt_money(-chargeback_net),
    }


# Per-unit net for rate items (storage $/GB-month, bandwidth $/GB):
# the percentage-fee scenarios apply per-unit; the fixed $0.30 fee
# applies once per invoice not per unit, so per-unit "net" omits it.
# Refund/chargeback per-unit are misleading because those events
# happen at the INVOICE level — so we mark them n/a and explain in
# the item's feeNote.
def _net_per_unit_by_method(rate_usd: float) -> dict:
    return {
        "usCard": _fmt_money(rate_usd * (1 - billing_lib.STRIPE_FEE_PCT_US_CARD)),
        "intlCardUsd": _fmt_money(
            rate_usd
            * (
                1
                - billing_lib.STRIPE_FEE_PCT_US_CARD
                - billing_lib.STRIPE_FEE_PCT_INTL_CARD_SURCHARGE
            )
        ),
        "intlCardNonUsd": _fmt_money(
            rate_usd
            * (
                1
                - billing_lib.STRIPE_FEE_PCT_US_CARD
                - billing_lib.STRIPE_FEE_PCT_INTL_CARD_SURCHARGE
                - billing_lib.STRIPE_FEE_PCT_CURRENCY_CONVERSION
            )
        ),
        "ach": _fmt_money(rate_usd * (1 - billing_lib.STRIPE_FEE_PCT_ACH)),
        "refund": "see invoice",
        "chargeback": "see invoice",
    }


def _fee_per_unit_by_method(rate_usd: float) -> dict:
    """Parallel to _net_per_unit_by_method but returns the per-unit
    FEE amount (what Stripe takes) per scenario. Refund/chargeback
    are invoice-level events so the per-unit version is meaningless;
    matches the "see invoice" pattern from the net helper."""
    return {
        "usCard": _fmt_money(rate_usd * billing_lib.STRIPE_FEE_PCT_US_CARD),
        "intlCardUsd": _fmt_money(
            rate_usd
            * (
                billing_lib.STRIPE_FEE_PCT_US_CARD
                + billing_lib.STRIPE_FEE_PCT_INTL_CARD_SURCHARGE
            )
        ),
        "intlCardNonUsd": _fmt_money(
            rate_usd
            * (
                billing_lib.STRIPE_FEE_PCT_US_CARD
                + billing_lib.STRIPE_FEE_PCT_INTL_CARD_SURCHARGE
                + billing_lib.STRIPE_FEE_PCT_CURRENCY_CONVERSION
            )
        ),
        "ach": _fmt_money(rate_usd * billing_lib.STRIPE_FEE_PCT_ACH),
        "refund": "see invoice",
        "chargeback": "see invoice",
    }


_REVENUE_MEMBERSHIP_ITEMS = [
    {
        "name": "Flow",
        "state": "active",
        "gross": "$1.00 per subscriber per year",
        "feeNetByMethod": _net_by_method(1.00),
        "feeAmountByMethod": _fee_by_method(1.00),
        "feeNote": None,
        # Internal fields (not displayed in the businessman-facing UI;
        # kept here so the code can still reference them).
        "mechanism": "Stripe Subscription",
        "trigger": "First card-add (immediate) + each signup anniversary",
        "codeRef": "billing.create_membership_subscription",
        "note": "",
    },
]

_REVENUE_USAGE_ITEMS = [
    {
        "name": "Storage",
        "state": "active",
        "gross": f"${billing_lib.PRICE_PER_GB_PER_MONTH_USD:.2f} per GB-month",
        "feeNetByMethod": _net_per_unit_by_method(
            billing_lib.PRICE_PER_GB_PER_MONTH_USD
        ),
        "feeAmountByMethod": _fee_per_unit_by_method(
            billing_lib.PRICE_PER_GB_PER_MONTH_USD
        ),
        "feeNote": "Per-GB net assumes a typical invoice size where the fixed 30¢ Stripe fee is spread across many GB. On a $5 invoice (current minimum) the real net is roughly $4.55 on US card, $4.96 on ACH; larger invoices get even closer to the per-unit rate above.",
        # Internal fields (not displayed).
        "mechanism": "Stripe Invoice",
        "trigger": "Monthly cron (3rd of month) when accrual crosses $5 — UNDER REDESIGN",
        "codeRef": "scripts/bill.py",
        "note": "Charged for each gigabyte of archived video stored, per month. This is the Basic-tier rate; usage is priced ~wholesale (cover cost plus a small buffer) and the tier subscriptions drive the profit. Per-tier: Basic $0.02, Creator $0.01, Studio $0.0075 / GB-month. Billed in monthly invoices once accrued usage crosses $5; timing is under active redesign.",
    },
    {
        "name": "Bandwidth (download)",
        "state": "active",
        "gross": (
            "Free"
            if billing_lib.DOWNLOAD_PRICE_PER_GB_USD == 0
            else f"${billing_lib.DOWNLOAD_PRICE_PER_GB_USD:.2f} per GB downloaded"
        ),
        "feeNetByMethod": _net_per_unit_by_method(
            billing_lib.DOWNLOAD_PRICE_PER_GB_USD
        ),
        "feeAmountByMethod": _fee_per_unit_by_method(
            billing_lib.DOWNLOAD_PRICE_PER_GB_USD
        ),
        "feeNote": "Downloads are free, so there's nothing to net out.",
        # Internal fields (not displayed).
        "mechanism": "None - downloads are free",
        "trigger": "n/a",
        "codeRef": "billing.DOWNLOAD_PRICE_PER_GB_USD = 0.0",
        "note": "Downloads are free. They stream B2 -> the Cloudflare Worker (dl.archive336.com) -> user, which costs us nothing (Bandwidth Alliance free egress), so we don't bill for it.",
    },
]

_REVENUE_ADDON_ITEMS = [
    {
        "name": "Small-charge surcharge",
        "state": "active",
        "gross": "$0.55 flat on sub-$5 final charges",
        "feeNetByMethod": {
            "usCard": "n/a*",
            "intlCardUsd": "n/a*",
            "intlCardNonUsd": "n/a*",
            "ach": "n/a*",
            "refund": "n/a*",
            "chargeback": "n/a*",
        },
        "feeAmountByMethod": {
            "usCard": "n/a",
            "intlCardUsd": "n/a",
            "intlCardNonUsd": "n/a",
            "ach": "n/a",
            "refund": "n/a",
            "chargeback": "n/a",
        },
        "feeNote": "The 55¢ exists to absorb Stripe's transaction fee on tiny invoices, not as a profit line. It's added to a sub-$5 final invoice (e.g. $3 of storage + 55¢ = $3.55 total) so the combined invoice ends up net-positive after Stripe takes its cut. Per-method net values aren't meaningful for the surcharge alone — the math only works at the combined-invoice level.",
        # Internal fields (not displayed).
        "mechanism": "Stripe InvoiceItem (added to final invoice)",
        "trigger": "Account deletion when outstanding storage < $5",
        "codeRef": "billing.compute_final_charge",
        "note": "Only fires when a user closes their account with under $5 of accrued storage. Regular monthly invoicing already requires $5+ to fire, so this is rare. Without it we'd lose money sending a final bill for a few cents.",
    },
]


def _revenue_membership_live_summary() -> Dict[str, Any]:
    """Pull subscriber count + annual gross + annual/monthly net.

    Reuses the same admin_billing_snapshot() that the /billing
    endpoint uses, so the subscriber count can't drift between the
    two surfaces. Annual gross = subscribers × ANNUAL_FEE_USD.
    Annual net assumes US-card payment (the dominant case per the
    refund/fee discussion in chat) — per-subscriber fee is fixed
    at $0.30 + 2.9% on each $1 charge, so total fee = subscribers
    × $0.329 and annual net = subscribers × $0.671. Monthly net is
    annual net ÷ 12.

    Quiet failure if Stripe is unreachable — the live summary is a
    bonus, not the source of truth for the static document.
    """
    out: Dict[str, Any] = {
        "subscriberCount": None,
        "annualGrossUsd": None,
        "annualNetUsd": None,
        "monthlyNetUsd": None,
    }
    price_id = os.environ.get("STRIPE_MEMBERSHIP_PRICE_ID", "")
    if not price_id:
        return out
    try:
        snap = billing_lib.admin_billing_snapshot(price_id)
        subs = snap.get("subscriptions", {})
        sub_count = (
            subs.get("active", 0) + subs.get("pastDue", 0) + subs.get("trialing", 0)
        )
        annual_gross = sub_count * billing_lib.ANNUAL_FEE_USD
        # Per-charge net under the US-card fee — applied per-subscriber
        # because each subscriber gets their own annual $1 charge with
        # its own fixed-fee component.
        per_sub_net = billing_lib.stripe_net_us_card(billing_lib.ANNUAL_FEE_USD)
        annual_net = sub_count * per_sub_net
        out["subscriberCount"] = sub_count
        out["annualGrossUsd"] = round(annual_gross, 2)
        out["annualNetUsd"] = round(annual_net, 2)
        out["monthlyNetUsd"] = round(annual_net / 12.0, 2)
    except Exception:  # noqa: BLE001
        log.exception("revenue membership live summary failed")
    return out


def _revenue_usage_live_summary(db: Session) -> Dict[str, Any]:
    """Sum total storage across all users + (placeholder) MTD bandwidth.

    Storage comes from the same R2 bucket-stats call /system uses.
    Bandwidth is unimplemented; left as null with an explanatory hint.
    """
    out: Dict[str, Any] = {
        "totalGbStored": None,
        "totalGbDownloadedMtd": None,
        "downloadNote": "Download tracking is not yet active — this number will populate once we start recording each user-initiated download.",
    }
    try:
        from app import ops_ledger
        stats = r2.bucket_stats(subject=ops_ledger.PLATFORM)
        if stats is not None:
            out["totalGbStored"] = round(stats["bytes"] / 1_000_000_000.0, 3)
    except Exception:  # noqa: BLE001
        log.exception("revenue usage live summary failed")
    return out


def _revenue_addon_live_summary() -> Dict[str, Any]:
    """Surcharges-fired count for the last 30d. Quietly returns null
    if we don't have a clean way to count them yet (small-charge
    surcharges are rare enough that a Stripe-side count by InvoiceItem
    description would work but isn't worth the cost yet).
    """
    return {
        "surchargesLast30d": None,
        "note": "Only fires when a user closes their account with under $5 of outstanding storage. Rare event; not pre-computed.",
    }


@router.get("/revenue")
def revenue(
    db: Session = Depends(get_db),
    _admin: User = Depends(get_admin_user),
) -> Dict[str, Any]:
    """Static revenue model document, organized by business-model
    framing (Membership / Usage / Add-ons) with billing-mechanism
    visible as a per-item field.

    Each bucket also returns a `liveSummary` with the most relevant
    operational number(s) computed at request time from existing
    sources — so the same surface serves both pricing-strategy
    thinking and operational sanity-checks.

    See module-level _REVENUE_MEMBERSHIP_ITEMS / _REVENUE_USAGE_ITEMS
    / _REVENUE_ADDON_ITEMS for the source of truth. Sibling of
    /api/admin/expenses on the inflow side.
    """
    return {
        "membership": {
            "subtitle": "Platform-access fees. What users pay just to have an account, independent of how much they use. Total scales with the number of paying subscribers.",
            "items": _REVENUE_MEMBERSHIP_ITEMS,
            "liveSummary": _revenue_membership_live_summary(),
        },
        "usage": {
            "subtitle": "Pay-as-you-go fees for what each user actually consumes. Storage scales with how many gigabytes they keep archived over time; bandwidth scales with how much they download.",
            "items": _REVENUE_USAGE_ITEMS,
            "liveSummary": _revenue_usage_live_summary(db),
        },
        "addOns": {
            "subtitle": "One-off fees that sit on top of the base pricing. Currently just the small-charge surcharge used to keep tiny final invoices from losing money to processor fees.",
            "items": _REVENUE_ADDON_ITEMS,
            "liveSummary": _revenue_addon_live_summary(),
        },
    }


# ---------- /live : live cost rollup (MTD numbers, subscriber math) -


@router.get("/live")
def live(
    db: Session = Depends(get_db),
    _admin: User = Depends(get_admin_user),
) -> Dict[str, Any]:
    """Live cost rollup — month-to-date metered numbers, subscriber
    count, coverage math against the static platform total.

    Counterpart to /api/admin/expenses (which is the static cost
    model document). The Live tab in admin polls this every 30s; the
    Expenses tab does not poll at all.

    Temporary single home for all live cost telemetry. Eventually
    redistributed: subscriber/coverage math to P&L or Billing tab,
    per-bucket metered breakdown to per-service AccountBoxes in
    Stack.
    """
    from sqlalchemy import func
    from app.models import R2OperationLog

    # Platform overhead total from the static cost model (sum of
    # active items only). Re-computed here so /live is independent.
    platform_total = round(
        sum(i["annualUsd"] for i in _PLATFORM_ITEMS if i["state"] == "active"),
        2,
    )

    # ---- Subscriber math ----
    annual_fee_usd = billing_lib.ANNUAL_FEE_USD
    subscriber_count = (
        db.query(User).filter(User.payment_status == "active").count()
    )
    coverage_usd = subscriber_count * annual_fee_usd
    uncovered_usd = max(0.0, platform_total - coverage_usd)
    break_even_subs = (
        int(-(-platform_total // annual_fee_usd))  # ceil
        if annual_fee_usd > 0
        else 0
    )
    recommended_fee_usd = (
        round(platform_total / subscriber_count, 2)
        if subscriber_count > 0
        else None
    )

    # ---- Metered MTD ----
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    storage_byte_hours_total = 0.0
    users = db.query(User).all()
    for u in users:
        # Shared-pool v2 is the authoritative byte-hour calc now;
        # see bill.py for the cutover rationale.
        bh = billing_lib.compute_user_byte_hours_v2(
            db, u.id, month_start, now
        )
        storage_byte_hours_total += bh
    storage_r2_cost = billing_lib.byte_hours_to_storage_cost_usd(
        storage_byte_hours_total
    )
    storage_billed = billing_lib.byte_hours_to_user_charge_usd(
        storage_byte_hours_total
    )

    ops_rows = (
        db.query(R2OperationLog.op_class, func.sum(R2OperationLog.count))
        .filter(
            R2OperationLog.subject != "__platform__",
            R2OperationLog.day >= month_start,
        )
        .group_by(R2OperationLog.op_class)
        .all()
    )
    ops_counts = {"A": 0, "B": 0}
    for cls, total in ops_rows:
        if cls in ops_counts:
            ops_counts[cls] = int(total or 0)
    ops_r2_cost = billing_lib.ops_to_r2_cost_usd(
        ops_counts["A"], ops_counts["B"]
    )
    ops_billed = billing_lib.ops_to_user_charge_usd(
        ops_counts["A"], ops_counts["B"]
    )

    metered_services = [
        {
            "name": "Backblaze storage (byte-hours)",
            "costUsd": round(storage_r2_cost, 4),
            "billedUsd": round(storage_billed, 4),
            "marginUsd": round(storage_billed - storage_r2_cost, 4),
            "detail": f"{storage_byte_hours_total / billing_lib.BYTES_PER_GB / billing_lib.HOURS_PER_MONTH_AVG:.4f} GB-months equivalent",
        },
        {
            "name": "Class A operations (free on B2)",
            "costUsd": round(
                (ops_counts["A"] / 1_000_000.0)
                * billing_lib.R2_CLASS_A_USD_PER_MILLION,
                4,
            ),
            "billedUsd": round(
                (ops_counts["A"] / 1_000_000.0)
                * billing_lib.R2_CLASS_A_USD_PER_MILLION
                * billing_lib.MARKUP,
                4,
            ),
            "marginUsd": round(
                (ops_counts["A"] / 1_000_000.0)
                * billing_lib.R2_CLASS_A_USD_PER_MILLION
                * (billing_lib.MARKUP - 1),
                4,
            ),
            "detail": f"{ops_counts['A']:,} ops",
        },
        {
            "name": "Class B operations (free on B2)",
            "costUsd": round(
                (ops_counts["B"] / 1_000_000.0)
                * billing_lib.R2_CLASS_B_USD_PER_MILLION,
                4,
            ),
            "billedUsd": round(
                (ops_counts["B"] / 1_000_000.0)
                * billing_lib.R2_CLASS_B_USD_PER_MILLION
                * billing_lib.MARKUP,
                4,
            ),
            "marginUsd": round(
                (ops_counts["B"] / 1_000_000.0)
                * billing_lib.R2_CLASS_B_USD_PER_MILLION
                * (billing_lib.MARKUP - 1),
                4,
            ),
            "detail": f"{ops_counts['B']:,} ops",
        },
    ]
    metered_cost_total = round(storage_r2_cost + ops_r2_cost, 4)
    metered_billed_total = round(storage_billed + ops_billed, 4)
    metered_margin_total = round(metered_billed_total - metered_cost_total, 4)
    metered_margin_pct = (
        round((metered_margin_total / metered_billed_total) * 100, 1)
        if metered_billed_total > 0
        else None
    )

    return {
        "platform": {
            "annualCostUsd": platform_total,
            "subscriberCount": subscriber_count,
            "currentAnnualFeeUsd": annual_fee_usd,
            "coverageUsd": round(coverage_usd, 2),
            "uncoveredUsd": round(uncovered_usd, 2),
            "breakEvenSubscribers": break_even_subs,
            "recommendedFeeUsd": recommended_fee_usd,
        },
        "metered": {
            "monthToDateCostUsd": metered_cost_total,
            "monthToDateBilledUsd": metered_billed_total,
            "monthToDateMarginUsd": metered_margin_total,
            "marginPct": metered_margin_pct,
            "markupMultiplier": billing_lib.MARKUP,
            "services": metered_services,
        },
        "asOf": now.isoformat(),
    }


# ---------- /errors : captured server + client error log ----------


# How much of the Python/JS stack to ship in the LIST response. Full
# stacks routinely run 5-20 KB and can blow up well beyond that on
# deep async chains; at the 500-row max limit the unbounded payload
# pushes the response into multi-megabyte territory. The list view
# only needs enough to recognize the error at a glance — the full
# trace is fetched on-demand via the per-row /errors/{id} endpoint
# when the operator expands a row.
_ERROR_STACK_LIST_BYTES = 2048


@router.get("/errors")
def list_errors(
    db: Session = Depends(get_db),
    _admin: User = Depends(get_admin_user),
    source: Optional[str] = Query(default=None, description="'server' | 'client'"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> Dict[str, Any]:
    """Recent captured errors, newest-first. Optional filter by source.

    Joins user_id -> username/email so the table can show who hit each
    error without a second round-trip per row.

    The ``stack`` field is truncated to ~2 KB per row to keep the list
    payload bounded; ``stackTruncated`` is True when the trace was cut.
    The full untruncated stack is available via GET /errors/{id}.
    """
    query = db.query(ErrorLog)
    if source in ("server", "client"):
        query = query.filter(ErrorLog.source == source)
    total = query.count()
    rows = (
        query.order_by(ErrorLog.created_at.desc()).offset(offset).limit(limit).all()
    )

    user_ids = {r.user_id for r in rows if r.user_id}
    label_by_user: Dict[str, Dict[str, str]] = {}
    if user_ids:
        for u in (
            db.query(User).filter(User.id.in_(list(user_ids))).all()
        ):
            label_by_user[u.id] = {"username": u.username, "email": u.email}

    items: List[Dict[str, Any]] = []
    for r in rows:
        meta = label_by_user.get(r.user_id) if r.user_id else None
        # Trim the stack to the list-view budget. Byte length not
        # character length, since Python stacks are ASCII in practice
        # and the budget exists to bound the response size on the wire.
        stack_full = r.stack
        stack_truncated = False
        if stack_full is not None and len(stack_full) > _ERROR_STACK_LIST_BYTES:
            stack_short = stack_full[:_ERROR_STACK_LIST_BYTES]
            stack_truncated = True
        else:
            stack_short = stack_full
        items.append(
            {
                "id": r.id,
                "userId": r.user_id,
                "username": meta["username"] if meta else None,
                "email": meta["email"] if meta else None,
                "source": r.source,
                "message": r.message,
                "stack": stack_short,
                "stackTruncated": stack_truncated,
                "requestPath": r.request_path,
                "requestMethod": r.request_method,
                "statusCode": r.status_code,
                "userAgent": r.user_agent,
                "createdAt": r.created_at.isoformat() if r.created_at else None,
            }
        )

    return {"total": total, "limit": limit, "offset": offset, "items": items}


@router.get("/errors/{error_id}")
def get_error(
    error_id: str,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_admin_user),
) -> Dict[str, Any]:
    """Return one error row with its full untruncated stack trace.

    Used by the admin Errors panel to lazy-load the full stack when an
    operator expands a row; keeps the list endpoint's payload bounded
    while preserving access to the full trace for debugging.
    """
    r = db.query(ErrorLog).filter(ErrorLog.id == error_id).first()
    if r is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Error row not found.",
        )

    # Resolve the user label same way the list endpoint does so the
    # expanded row can show the user context without a second hit.
    user_meta: Optional[Dict[str, str]] = None
    if r.user_id:
        u = db.query(User).filter(User.id == r.user_id).first()
        if u is not None:
            user_meta = {"username": u.username, "email": u.email}

    return {
        "id": r.id,
        "userId": r.user_id,
        "username": user_meta["username"] if user_meta else None,
        "email": user_meta["email"] if user_meta else None,
        "source": r.source,
        "message": r.message,
        "stack": r.stack,
        "stackTruncated": False,
        "requestPath": r.request_path,
        "requestMethod": r.request_method,
        "statusCode": r.status_code,
        "userAgent": r.user_agent,
        "createdAt": r.created_at.isoformat() if r.created_at else None,
    }


# ---------- /storage-billing-check : compare ledger vs legacy meter ----------


# Storage billing rate constants live in billing.py — single source of
# truth at the byte-hour granularity. This endpoint cross-checks the
# byte-hour ledger against the legacy daily-snapshot path; it imports
# from billing.py rather than redefining shadow constants.


@router.get("/storage-billing-check")
def storage_billing_check(
    user_id: Optional[str] = Query(None),
    days: int = Query(30, ge=1, le=365),
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Cross-check the new StorageObject byte-hour integral against the
    legacy UsageRecord daily-snapshot estimate.

    During Phase B/C/D of the storage-billing migration both paths run
    in parallel; this endpoint surfaces any drift so we can spot a
    miswiring before flipping the bill cron to the new path.

    Query params:
      - user_id: scope to one user, or omit for account-wide totals
      - days: lookback window (default 30, max 365)

    Returns USD totals from each method + their absolute and relative
    difference. The new (ledger) method is the source of truth going
    forward.
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)

    # --- New method: byte-hour integral over StorageObject ---
    q = db.query(StorageObject).filter(
        StorageObject.uploaded_at < now,
    )
    if user_id:
        q = q.filter(StorageObject.user_id == user_id)
    rows = q.all()

    byte_hours = 0.0
    overlap_objects = 0
    for r in rows:
        # Naive UTC for comparison since SQLite stores naive datetimes.
        up = r.uploaded_at
        if up.tzinfo is None:
            up = up.replace(tzinfo=timezone.utc)
        de = r.deleted_at
        if de is not None and de.tzinfo is None:
            de = de.replace(tzinfo=timezone.utc)

        effective_start = max(up, start)
        effective_end = min(de or now, now)
        if effective_end > effective_start:
            hours = (effective_end - effective_start).total_seconds() / 3600.0
            byte_hours += (r.bytes + r.metadata_bytes) * hours
            overlap_objects += 1

    ledger_r2_cost_usd = billing_lib.byte_hours_to_storage_cost_usd(byte_hours)
    ledger_user_charge_usd = billing_lib.byte_hours_to_user_charge_usd(byte_hours)

    # --- Legacy method: UsageRecord daily snapshots ---
    legacy_q = db.query(UsageRecord).filter(UsageRecord.day >= start)
    if user_id:
        legacy_q = legacy_q.filter(UsageRecord.user_id == user_id)
    legacy_rows = legacy_q.all()

    # Each row = bytes_stored across one day. Both paths now route
    # through the byte-hour rate (see billing.py) so the only thing
    # this endpoint surfaces is data drift (objects accounted in one
    # ledger but not the other), not rate-constant drift.
    legacy_gb_days = sum(
        r.bytes_stored / billing_lib.BYTES_PER_GB for r in legacy_rows
    )
    legacy_user_charge_usd = billing_lib.gb_days_to_usd(legacy_gb_days)
    legacy_r2_cost_usd = legacy_user_charge_usd / billing_lib.MARKUP

    diff_usd = ledger_user_charge_usd - legacy_user_charge_usd
    diff_pct = (
        (diff_usd / legacy_user_charge_usd * 100.0)
        if legacy_user_charge_usd > 0
        else 0.0
    )

    return {
        "userId": user_id,
        "days": days,
        "periodStart": start.isoformat(),
        "periodEnd": now.isoformat(),
        "ledger": {
            "objectsConsidered": len(rows),
            "objectsWithOverlap": overlap_objects,
            "byteHours": byte_hours,
            "r2CostUsd": round(ledger_r2_cost_usd, 6),
            "userChargeUsd": round(ledger_user_charge_usd, 6),
        },
        "legacy": {
            "rowsConsidered": len(legacy_rows),
            "gbDays": round(legacy_gb_days, 6),
            "r2CostUsd": round(legacy_r2_cost_usd, 6),
            "userChargeUsd": round(legacy_user_charge_usd, 6),
        },
        "diff": {
            "absUsd": round(diff_usd, 6),
            "pct": round(diff_pct, 3),
        },
    }


# ---------- /service-health : liveness + warnings per external service ----------


# The single Hetzner Cloud server we run on. Hardcoded because we only
# ever have one VM at our scale; if that changes we lift this into
# .env or read from the API's server list.
_HETZNER_SERVER_ID = 128288947


def _hetzner_bandwidth() -> Optional[Dict[str, int]]:
    """Return current-period outbound traffic + included cap (bytes).

    Hetzner resets these counters at the start of each billing cycle.
    Returns None if HCLOUD_TOKEN isn't configured or the API call
    fails. The UI shows "—" in those cases.
    """
    import requests

    token = os.environ.get("HCLOUD_TOKEN")
    if not token:
        return None
    try:
        resp = requests.get(
            f"https://api.hetzner.cloud/v1/servers/{_HETZNER_SERVER_ID}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if resp.status_code != 200:
            log.warning(
                "hetzner server fetch returned %d", resp.status_code
            )
            return None
        server = resp.json().get("server") or {}
        return {
            "outgoingBytes": int(server.get("outgoing_traffic") or 0),
            "includedBytes": int(server.get("included_traffic") or 0),
        }
    except Exception:
        log.warning("hetzner bandwidth fetch failed", exc_info=True)
        return None


def _hetzner_resource_count(path: str, list_key: str) -> Optional[int]:
    """Generic counter for any Hetzner Cloud resource list endpoint.

    Returns the integer count when HCLOUD_TOKEN is set and the API
    responds. Returns None on token-missing or any failure. We expect
    zero of these for our deployment — anything > 0 is a warning the
    UI surfaces with yellow emphasis + a "review" link to the console.

    Used by the volumes / floating_ips / load_balancers watchers
    below, in addition to the snapshot-via-images-filter call.
    """
    import requests

    token = os.environ.get("HCLOUD_TOKEN")
    if not token:
        return None
    try:
        resp = requests.get(
            f"https://api.hetzner.cloud/v1/{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if resp.status_code != 200:
            log.warning(
                "hetzner %s list returned %d", path, resp.status_code
            )
            return None
        return len(resp.json().get(list_key, []))
    except Exception:
        log.warning("hetzner %s count failed", path, exc_info=True)
        return None


def _hetzner_snapshot_count() -> Optional[int]:
    """Count manual snapshots via the Hetzner Cloud API.

    Snapshots live under the /images endpoint with type=snapshot
    (Hetzner uses one resource type for both manual snapshots and
    automated backup images, differentiated by the type filter).
    """
    import requests

    token = os.environ.get("HCLOUD_TOKEN")
    if not token:
        return None
    try:
        resp = requests.get(
            "https://api.hetzner.cloud/v1/images",
            params={"type": "snapshot"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if resp.status_code != 200:
            log.warning(
                "hetzner snapshot list returned %d", resp.status_code
            )
            return None
        data = resp.json()
        return len(data.get("images", []))
    except Exception:
        log.warning("hetzner snapshot count failed", exc_info=True)
        return None


def _hetzner_health() -> Dict[str, Any]:
    """Local box health: disk + memory + uptime + manual snapshots.
    The fact that this endpoint is responding implies Hetzner itself
    is up, so the interesting signal is "is anything close to a
    threshold."

    Warning thresholds:
      - disk free < 10% of total
      - memory used > 90% of total
      - manual snapshot count > 0 (we never expect any)
      - bandwidth used >= alerts.HETZNER_BANDWIDTH_WARNING_PCT of cap
    """
    import shutil

    from app import alerts

    status = "active"
    warnings: List[str] = []
    disk_free_gb: Optional[float] = None
    disk_used_pct: Optional[float] = None
    mem_used_pct: Optional[float] = None
    uptime_seconds: Optional[float] = None

    try:
        du = shutil.disk_usage("/")
        disk_free_gb = round(du.free / 1_073_741_824, 2)
        disk_used_pct = round((du.total - du.free) / du.total * 100, 1)
        if du.free / du.total < 0.10:
            status = "warning"
            warnings.append(f"disk free is {disk_free_gb} GB ({disk_used_pct}% used)")
    except Exception:
        log.exception("hetzner_health: disk read failed")

    try:
        with open("/proc/meminfo", "r") as f:
            meminfo = f.read()
        total_kb = available_kb = None
        for line in meminfo.splitlines():
            if line.startswith("MemTotal:"):
                total_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                available_kb = int(line.split()[1])
            if total_kb is not None and available_kb is not None:
                break
        if total_kb and available_kb:
            used_kb = total_kb - available_kb
            mem_used_pct = round(used_kb / total_kb * 100, 1)
            if used_kb / total_kb > 0.90:
                status = "warning"
                warnings.append(f"memory at {mem_used_pct}% used")
    except FileNotFoundError:
        # /proc/meminfo doesn't exist on macOS (local dev) — fine to skip.
        pass
    except Exception:
        log.exception("hetzner_health: meminfo read failed")

    try:
        with open("/proc/uptime", "r") as f:
            uptime_seconds = float(f.read().split()[0])
    except (FileNotFoundError, ValueError):
        pass
    except Exception:
        log.exception("hetzner_health: uptime read failed")

    # Manual snapshot watch. Returns None when HCLOUD_TOKEN isn't
    # configured (frontend shows "—"). Any positive count is a warning
    # because we never intend to have any — Hetzner Backups covers DR.
    manual_snapshots = _hetzner_snapshot_count()
    if manual_snapshots is not None and manual_snapshots > 0:
        status = "warning"
        warnings.append(
            f"{manual_snapshots} manual snapshot(s) present — review and delete"
        )

    # Other Hetzner resources we don't expect to exist. Any positive
    # count would represent accidental cost accumulation. Same pattern
    # as the snapshot watcher above.
    volume_count = _hetzner_resource_count("volumes", "volumes")
    if volume_count is not None and volume_count > 0:
        status = "warning"
        warnings.append(
            f"{volume_count} additional volume(s) present — ~$0.044/GB-mo each"
        )
    floating_ip_count = _hetzner_resource_count("floating_ips", "floating_ips")
    if floating_ip_count is not None and floating_ip_count > 0:
        status = "warning"
        warnings.append(
            f"{floating_ip_count} floating IP(s) present — $0.50/mo each"
        )
    load_balancer_count = _hetzner_resource_count(
        "load_balancers", "load_balancers"
    )
    if load_balancer_count is not None and load_balancer_count > 0:
        status = "warning"
        warnings.append(
            f"{load_balancer_count} load balancer(s) present — $5+/mo each"
        )

    # Bandwidth watch. Hetzner includes 2 TB outbound per billing
    # period on the CPX21 tier; overage is ~$1.07/TB. We warn at 50%
    # of the cap so the operator has runway to investigate before any
    # overage hits. Email alerting is handled by the separate
    # archive336-hetzner-bandwidth cron — this only sets the UI pill.
    bandwidth = _hetzner_bandwidth()
    if bandwidth and bandwidth["includedBytes"] > 0:
        used_pct = (
            bandwidth["outgoingBytes"] / bandwidth["includedBytes"] * 100
        )
        if used_pct >= alerts.HETZNER_BANDWIDTH_WARNING_PCT:
            status = "warning"
            warnings.append(
                f"bandwidth at {used_pct:.1f}% of monthly cap"
            )

    return {
        "status": status,
        "diskFreeGb": disk_free_gb,
        "diskUsedPct": disk_used_pct,
        "memUsedPct": mem_used_pct,
        "uptimeSeconds": uptime_seconds,
        "manualSnapshots": manual_snapshots,
        "volumes": volume_count,
        "floatingIps": floating_ip_count,
        "loadBalancers": load_balancer_count,
        "bandwidth": bandwidth,
        "warnings": warnings,
    }


def _sentry_health() -> Dict[str, Any]:
    """Event-count summary for the Sentry AccountBox.

    Calls Sentry's stats_v2 endpoint for the current month's error
    event count. Free read-only auth token (Internal Integration with
    org:read + event:read scope, or User Auth Token with org:read).
    No per-call cost. Returns None for eventsThisMonth when
    SENTRY_AUTH_TOKEN isn't configured — UI shows "—" in that case.

    Free tier: 5,000 errors/mo. Warning pill flips at
    alerts.SENTRY_WARNING_THRESHOLD (2,500 = 50% of cap). This
    endpoint only sets the UI status; the email alert is driven by
    the daily scripts/check_sentry_quota.py cron so the operator
    gets notified independently of whether anyone has the admin tab
    open. Sentry's cap is more dangerous than Resend's: hitting it
    means we LOSE visibility into further errors (silent failure).
    """
    import requests

    from app import alerts

    monthly_cap = 5000
    org_slug = "archive336"
    status = "active"
    warnings: List[str] = []
    events_this_month: Optional[int] = None

    token = os.environ.get("SENTRY_AUTH_TOKEN")
    if not token:
        return {
            "status": status,
            "eventsThisMonth": None,
            "monthlyCap": monthly_cap,
            "warnings": warnings,
        }

    now = datetime.now(timezone.utc)
    month_start = now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    try:
        resp = requests.get(
            f"https://sentry.io/api/0/organizations/{org_slug}/stats_v2/",
            params={
                "field": "sum(quantity)",
                "category": "error",
                "start": month_start.isoformat().replace("+00:00", "Z"),
                "end": now.isoformat().replace("+00:00", "Z"),
                "interval": "1d",
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if resp.status_code != 200:
            log.warning("sentry stats returned %d", resp.status_code)
            return {
                "status": status,
                "eventsThisMonth": None,
                "monthlyCap": monthly_cap,
                "warnings": warnings,
            }
        data = resp.json()
        # stats_v2 returns groups with totals; sum across groups in case
        # there are multiple (we filter by category=error which usually
        # collapses to one group, but be defensive).
        events_this_month = 0
        for group in data.get("groups") or []:
            events_this_month += int(
                group.get("totals", {}).get("sum(quantity)") or 0
            )
        if events_this_month >= alerts.SENTRY_WARNING_THRESHOLD:
            status = "warning"
            warnings.append(
                f"events this month {events_this_month}/{monthly_cap}"
            )
    except Exception:
        log.warning("sentry events fetch failed", exc_info=True)

    return {
        "status": status,
        "eventsThisMonth": events_this_month,
        "monthlyCap": monthly_cap,
        "warnings": warnings,
    }


def _resend_health(db: Session) -> Dict[str, Any]:
    """Send-volume summary for the Resend AccountBox.

    Reads from our own EmailSendLog (no Resend API call needed — same
    reasoning as /api/admin/system: avoids requiring a higher-scope
    Resend API key just to surface a quota number).

    Note: our EmailSendLog rows persist across Resend API-key rotations
    so a count here can drift above what Resend's dashboard shows if
    you ever moved workspaces. Visible discrepancy is informational,
    not a bug.

    Free tier: 100/day · 3000/month. Warning at 80% of either cap.
    """
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    sends_today = (
        db.query(EmailSendLog)
        .filter(EmailSendLog.created_at >= today_start)
        .count()
    )
    sends_this_month = (
        db.query(EmailSendLog)
        .filter(EmailSendLog.created_at >= month_start)
        .count()
    )

    daily_cap = 100
    monthly_cap = 3000
    status = "active"
    warnings: List[str] = []
    if sends_today >= daily_cap * 0.8:
        status = "warning"
        warnings.append(f"sends today {sends_today}/{daily_cap}")
    if sends_this_month >= monthly_cap * 0.8:
        status = "warning"
        warnings.append(f"sends this month {sends_this_month}/{monthly_cap}")

    return {
        "status": status,
        "sendsToday": sends_today,
        "sendsThisMonth": sends_this_month,
        "dailyCap": daily_cap,
        "monthlyCap": monthly_cap,
        "warnings": warnings,
    }


def _backblaze_health() -> Dict[str, Any]:
    """Backblaze B2 object-storage stats for the Stack tab's Backblaze box.

    Storage bytes + object count come from the cached B2 bucket walk
    (cloudflare._b2_user_storage, ~5 min TTL, so the 30s health poll doesn't
    LIST the bucket every time). Operational only - cost lives in Expenses.
    """
    bucket = os.environ.get("STORAGE_BUCKET")
    if not bucket:
        return {"status": "active", "configured": False}
    from app import cloudflare as cf_lib  # noqa: WPS433

    stats = cf_lib._b2_user_storage()  # {objects, bytes} or None
    return {
        "status": "active",
        "configured": True,
        "bucket": bucket,
        "region": os.environ.get("STORAGE_REGION"),
        "endpoint": os.environ.get("STORAGE_ENDPOINT"),
        "storageBytes": stats["bytes"] if stats else None,
        "objectCount": stats["objects"] if stats else None,
    }


@router.get("/service-health")
def service_health(
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Liveness + warning signals for each external service we depend on.

    Powers the status pills in the admin Stack tab. Each entry has
    at least { status: 'active'|'warning'|'down' } plus service-
    specific detail fields. Frontend polls this periodically and
    re-renders the AccountBox pills.

    Other services (Stripe, R2, ...) will be added here as we migrate
    them into AccountBoxes.
    """
    from app import cloudflare as cf_lib

    return {
        "hetzner": _hetzner_health(),
        "resend": _resend_health(db),
        "sentry": _sentry_health(),
        "cloudflare": cf_lib.cloudflare_health(),
        "backblaze": _backblaze_health(),
    }


# ---------- /reconciliation : storage reconciliation log ----------


@router.get("/reconciliation")
def reconciliation_log(
    days: int = Query(7, ge=1, le=365),
    user_id: Optional[str] = Query(None),
    action: Optional[str] = Query(None, pattern="^(delete_orphan|mark_phantom|fix_drift)$"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Recent reconciliation actions (orphan deletes, phantom marks,
    drift fixes). Surfaces what the daily reconcile cron has been
    doing so we can spot anything weird before it compounds.

    Query params:
      - days: lookback window (default 7, max 365)
      - user_id: scope to one user
      - action: filter to one action type
      - limit: max rows returned (default 100, max 1000). Tightened
        from the previous 500/5000 ceiling so the per-page payload
        stays bounded even when details_json is large.
      - offset: pagination offset, paired with limit for "load more"

    Per-row details_json is replaced by a lightweight summary
    (top-level key list + byte size). The full object is available
    via GET /reconciliation/{id} when the operator opens a row.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    q = db.query(ReconciliationLog).filter(ReconciliationLog.ran_at >= cutoff)
    if user_id:
        q = q.filter(ReconciliationLog.user_id == user_id)
    if action:
        q = q.filter(ReconciliationLog.action == action)
    # Total = rows matching the filter inside the window. This is the
    # number a paging UI uses to render "showing 100 of 423".
    total = q.count()
    rows = (
        q.order_by(ReconciliationLog.ran_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    # Summary counts across the page itself, NOT the whole window.
    # If a "load more" UI walks every page the running totals add up.
    summary = {"delete_orphan": 0, "mark_phantom": 0, "fix_drift": 0}
    alerted_count = 0
    for r in rows:
        if r.action in summary:
            summary[r.action] += 1
        if r.alerted:
            alerted_count += 1

    items = []
    for r in rows:
        # Build a stub for the details column so the list payload
        # stays bounded. We keep just enough metadata that an operator
        # can decide whether to drill into the row: the top-level key
        # set and the raw byte size of the JSON blob. The full
        # details object is fetched on demand via the per-row endpoint.
        details_keys: List[str] = []
        details_size = 0
        if r.details_json:
            details_size = len(r.details_json)
            try:
                parsed = json.loads(r.details_json)
                if isinstance(parsed, dict):
                    details_keys = sorted(str(k) for k in parsed.keys())
                # Non-dict JSON (list, string, number) - leave keys
                # empty; the size signal is enough to tell the operator
                # there's content worth fetching.
            except (json.JSONDecodeError, TypeError):
                # Malformed details_json. Leave keys empty; size still
                # tells the operator the row has weird content worth a
                # full fetch.
                pass
        items.append({
            "id": r.id,
            "userId": r.user_id,
            "action": r.action,
            "r2Key": r.r2_key,
            "detailsKeys": details_keys,
            "detailsSize": details_size,
            "ranAt": r.ran_at.isoformat() if r.ran_at else None,
            "alerted": r.alerted,
        })

    return {
        "days": days,
        "limit": limit,
        "offset": offset,
        "total": total,
        # Kept for backward-compat with any caller still reading
        # response.count - same value as len(items) by construction.
        "count": len(rows),
        "summary": summary,
        "alertedCount": alerted_count,
        "items": items,
    }


@router.get("/reconciliation/{recon_id}")
def get_reconciliation(
    recon_id: str,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Return one reconciliation row with the full parsed details_json.

    Pairs with the list endpoint, which now ships a key-summary stub
    instead of the unbounded details blob. UI calls this when the
    operator opens a row to see the full payload.
    """
    r = (
        db.query(ReconciliationLog)
        .filter(ReconciliationLog.id == recon_id)
        .first()
    )
    if r is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Reconciliation row not found.",
        )

    details: Any
    try:
        details = json.loads(r.details_json) if r.details_json else None
    except (json.JSONDecodeError, TypeError):
        # Pass the raw string through so the operator can still see
        # what was stored even when it isn't valid JSON.
        details = r.details_json

    return {
        "id": r.id,
        "userId": r.user_id,
        "action": r.action,
        "r2Key": r.r2_key,
        "details": details,
        "ranAt": r.ran_at.isoformat() if r.ran_at else None,
        "alerted": r.alerted,
    }


# ---------- /stack : render STACK.md ----------


@router.get("/stack")
def stack_doc(
    _admin: User = Depends(get_admin_user),
) -> Dict[str, str]:
    """Return the raw contents of STACK.md from the repo so the admin
    UI can render it as the single source of truth for service deps.
    """
    # STACK.md lives at the repo root. backend/app/routes/admin.py is
    # 3 levels deep, so .../../../STACK.md from this file's location.
    here = os.path.dirname(os.path.abspath(__file__))
    stack_path = os.path.normpath(os.path.join(here, "..", "..", "..", "STACK.md"))
    try:
        with open(stack_path, "r", encoding="utf-8") as f:
            return {"markdown": f.read()}
    except FileNotFoundError:
        log.warning("STACK.md not found at %s", stack_path)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="STACK.md not found in deploy.",
        )
