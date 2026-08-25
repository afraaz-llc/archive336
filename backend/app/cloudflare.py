"""Cloudflare API helpers for the admin service-health endpoint.

We pull a few pieces of live data from Cloudflare:

  - DNS record count (vs the 200-records-per-zone Free-plan cap)
  - Email Routing rule count (vs the 200-rules cap)
  - Bandwidth, request count, cache hit ratio (24h + month-to-date)
  - Threat / WAF block count

All of this is "interesting to know" rather than "billable" — Cloudflare
doesn't bill us for any of these on Free (see docs/CLOUDFLARE_AUDIT.md
for the full audit). The point of pulling them is:

  1. Catch unexpected growth toward Free-tier caps before it's a
     problem (DNS records, Email Routing rules).
  2. Watch cache hit ratio as a leading indicator of Hetzner egress
     pressure (cache misses = Hetzner bandwidth bill, not Cloudflare).
  3. Surface threat counts so we notice abuse early.

Every helper here is best-effort: if CLOUDFLARE_API_TOKEN isn't set, or
the API call fails, we return None and the UI shows "—". The admin
panel never crashes because Cloudflare's API blipped.

Token requirements (minimum scope, per audit doc §11):
  - Zone > Analytics > Read
  - Zone > Zone > Read
  - Zone > DNS > Read
  - Zone > Email Routing Rules > Read
  - Zone resources: include this zone only

Env vars (read at call time, not import time, so service can boot
without them and degrade gracefully):
  - CLOUDFLARE_API_TOKEN — Bearer token (cfut_... format), zone-scoped
    read on Analytics + Zone + DNS + Email Routing for the
    archive336.com zone only
  - CLOUDFLARE_ZONE_ID   — zone tag, hex 32-char (looked up once and
    pinned in .env; we could resolve it via /zones?name=... but that
    requires Zone:Read on the whole account which is broader scope
    than we want)
  - CLOUDFLARE_ACCOUNT_ANALYTICS_TOKEN — separate Bearer token scoped
    to Account > Account Analytics > Read. Required for the R2 ops
    GraphQL dataset (``r2OperationsAdaptiveGroups``) since it's an
    account-level dataset and the zone-scoped token above can't see
    it. Kept as a separate token so we can rotate R2 monitoring
    independently of DNS/email reads.
  - CLOUDFLARE_ACCOUNT_ID — account tag, hex 32-char, pinned in .env
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import requests


log = logging.getLogger("archive336.cloudflare")


_BASE = "https://api.cloudflare.com/client/v4"
_GRAPHQL = f"{_BASE}/graphql"
_TIMEOUT = 5  # seconds — short on purpose; we'd rather degrade than block.


def _token() -> Optional[str]:
    return os.environ.get("CLOUDFLARE_API_TOKEN") or None


def _zone_id() -> Optional[str]:
    return os.environ.get("CLOUDFLARE_ZONE_ID") or None


def _account_analytics_token() -> Optional[str]:
    """Separate token with Account > Account Analytics > Read.

    Required for the R2 ops dataset (r2OperationsAdaptiveGroups)
    since it's account-scoped, not zone-scoped. Returning None here
    causes ``r2_ops_by_bucket`` and the reconciliation logic to
    degrade gracefully — the box just won't show CF-side counts.
    """
    return os.environ.get("CLOUDFLARE_ACCOUNT_ANALYTICS_TOKEN") or None


def _account_id() -> Optional[str]:
    return os.environ.get("CLOUDFLARE_ACCOUNT_ID") or None


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
    }


def _account_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_account_analytics_token()}",
        "Content-Type": "application/json",
    }


# S3 API operation → R2 billing class mapping. Source: docs/CLOUDFLARE_AUDIT.md §2.
# Anything NOT in either set is either free (DeleteObject,
# AbortMultipartUpload, DeleteBucket) or unknown (a new op type
# Cloudflare adds — we count it under 'unknown' in the breakdown so
# we notice it). Frozen sets so lookup is O(1).
R2_CLASS_A_OPS = frozenset({
    "PutObject", "CopyObject", "CompleteMultipartUpload",
    "CreateMultipartUpload", "LifecycleStorageTierTransition",
    "ListMultipartUploads", "UploadPart", "UploadPartCopy", "ListParts",
    "PutBucketEncryption", "PutBucketCors",
    "PutBucketLifecycleConfiguration", "ListBuckets", "PutBucket",
    "ListObjects", "ListObjectsV2",
})
R2_CLASS_B_OPS = frozenset({
    "HeadBucket", "HeadObject", "GetObject", "UsageSummary",
    "GetBucketEncryption", "GetBucketLocation", "GetBucketCors",
    "GetBucketLifecycleConfiguration", "GetBucketSippyConfiguration",
})
R2_FREE_OPS = frozenset({
    "DeleteObject", "DeleteObjects", "AbortMultipartUpload",
    "DeleteBucket",
})


def _classify_r2_op(action_type: str) -> str:
    """Map a Cloudflare actionType to 'A', 'B', 'free', or 'unknown'."""
    if action_type in R2_CLASS_A_OPS:
        return "A"
    if action_type in R2_CLASS_B_OPS:
        return "B"
    if action_type in R2_FREE_OPS:
        return "free"
    return "unknown"


# --------------------------------------------------------------- DNS

# Hard cap on Free plan for zones created on/after 2024-09-01.
# Older zones are grandfathered to 1000. Ours is post-2024 so 200
# applies. See docs/CLOUDFLARE_AUDIT.md §3.1.
DNS_RECORDS_CAP = 200


def dns_record_count() -> Optional[int]:
    """Total DNS records in the zone (vs DNS_RECORDS_CAP).

    Uses the per_page=1 + result_info.total_count trick so we get
    the count without fetching all the record bodies.
    """
    token = _token()
    zid = _zone_id()
    if not token or not zid:
        return None
    try:
        r = requests.get(
            f"{_BASE}/zones/{zid}/dns_records",
            params={"per_page": 1},
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            log.warning("cloudflare DNS list returned %d", r.status_code)
            return None
        info = r.json().get("result_info") or {}
        n = info.get("total_count")
        return int(n) if n is not None else None
    except Exception:
        # warning (not exception) so an expected Cloudflare API timeout
        # during a health poll stays a breadcrumb instead of a Sentry
        # event - event_level is ERROR. Matches the Hetzner health
        # helpers; third-party slowness isn't an app bug worth alerting.
        log.warning("cloudflare DNS count failed", exc_info=True)
        return None


# ----------------------------------------------------- Email Routing

EMAIL_ROUTING_RULES_CAP = 200


def email_routing_rule_count() -> Optional[int]:
    """Total Email Routing rules in the zone (vs the 200 cap).

    Same per_page=1 trick. Note the count includes the catch-all rule
    even though it's surfaced separately in the dashboard UI.
    """
    token = _token()
    zid = _zone_id()
    if not token or not zid:
        return None
    try:
        r = requests.get(
            f"{_BASE}/zones/{zid}/email/routing/rules",
            params={"per_page": 1},
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            log.warning("cloudflare email rules list returned %d", r.status_code)
            return None
        info = r.json().get("result_info") or {}
        n = info.get("total_count")
        return int(n) if n is not None else None
    except Exception:
        log.warning("cloudflare email routing rule count failed", exc_info=True)
        return None


# --------------------------------------------------- GraphQL analytics


_BANDWIDTH_QUERY = """
query Bandwidth($zoneTag: String!, $since: Time!, $until: Time!) {
  viewer {
    zones(filter: {zoneTag: $zoneTag}) {
      httpRequests1hGroups(
        limit: 1000
        filter: {datetime_geq: $since, datetime_lt: $until}
      ) {
        sum { bytes cachedBytes requests cachedRequests threats }
      }
    }
  }
}
"""


def _gql_bandwidth(since: datetime, until: datetime) -> Optional[Dict[str, int]]:
    """Internal: sum bandwidth + requests over an hourly window.

    Returns a dict with byte / request / threat totals, or None.
    """
    token = _token()
    zid = _zone_id()
    if not token or not zid:
        return None
    try:
        r = requests.post(
            _GRAPHQL,
            headers=_headers(),
            timeout=_TIMEOUT,
            json={
                "query": _BANDWIDTH_QUERY,
                "variables": {
                    "zoneTag": zid,
                    "since": since.strftime("%Y-%m-%dT%H:00:00Z"),
                    "until": until.strftime("%Y-%m-%dT%H:00:00Z"),
                },
            },
        )
        if r.status_code != 200:
            log.warning("cloudflare graphql returned %d", r.status_code)
            return None
        body = r.json()
        if body.get("errors"):
            log.warning("cloudflare graphql errors: %s", body["errors"])
            return None
        groups = (
            body.get("data", {})
            .get("viewer", {})
            .get("zones", [{}])[0]
            .get("httpRequests1hGroups", [])
            or []
        )
        out = {
            "bytes": 0,
            "cachedBytes": 0,
            "requests": 0,
            "cachedRequests": 0,
            "threats": 0,
        }
        for g in groups:
            s = g.get("sum") or {}
            out["bytes"] += int(s.get("bytes") or 0)
            out["cachedBytes"] += int(s.get("cachedBytes") or 0)
            out["requests"] += int(s.get("requests") or 0)
            out["cachedRequests"] += int(s.get("cachedRequests") or 0)
            out["threats"] += int(s.get("threats") or 0)
        return out
    except Exception:
        log.warning("cloudflare graphql bandwidth fetch failed", exc_info=True)
        return None


def bandwidth_24h() -> Optional[Dict[str, int]]:
    """Sum bandwidth + requests for the last full 24 hours.

    The Free-plan analytics floor is hourly granularity, so this is
    the finest-grained view we get without paying for Pro.
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=24)
    return _gql_bandwidth(since, now)


def bandwidth_last_3d() -> Optional[Dict[str, int]]:
    """Sum bandwidth + requests for the last 3 days (the Free-plan max
    for the httpRequests1hGroups dataset — confirmed empirically when
    a 30-day query came back with `code: quota` on this zone).

    For a true "this month" number we'd need Pro plan analytics OR
    we'd have to roll up daily snapshots ourselves. Both are
    deferred — `last24h` is the live signal, this is the wider
    context.
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=3)
    return _gql_bandwidth(since, now)


# -------------- R2 ops reconciliation (CF-side ground truth) ---------


_R2_OPS_QUERY = """
query R2Ops($accountTag: string!, $since: Time!, $until: Time!) {
  viewer {
    accounts(filter: {accountTag: $accountTag}) {
      r2OperationsAdaptiveGroups(
        limit: 10000,
        filter: {datetime_geq: $since, datetime_leq: $until}
      ) {
        sum { requests }
        dimensions { bucketName, actionType, actionStatus }
      }
    }
  }
}
"""


def r2_ops_by_bucket(
    since: datetime, until: datetime
) -> Optional[Dict[str, Dict[str, int]]]:
    """Per-bucket Class A / B / free op counts from Cloudflare's
    authoritative R2 metrics, summed over [since, until).

    Returns shape: ``{bucketName: {classA: N, classB: N, free: N,
    unknown: N}, ...}`` or None when the account-analytics token isn't
    configured / the GraphQL call fails.

    Used by the reconciliation logic to cross-check our ledger
    (R2OperationLog) against ground truth. Drift between the two
    surfaces as a warning on the CloudflareAccountBox.

    Retention on this dataset is 31 days regardless of plan, so we
    can query the full month without hitting Free-plan caps.
    """
    token = _account_analytics_token()
    acct = _account_id()
    if not token or not acct:
        return None
    try:
        r = requests.post(
            _GRAPHQL,
            headers=_account_headers(),
            timeout=_TIMEOUT,
            json={
                "query": _R2_OPS_QUERY,
                "variables": {
                    "accountTag": acct,
                    "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "until": until.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
            },
        )
        if r.status_code != 200:
            log.warning("cloudflare r2 ops graphql returned %d", r.status_code)
            return None
        body = r.json()
        if body.get("errors"):
            log.warning("cloudflare r2 ops graphql errors: %s", body["errors"])
            return None
        groups = (
            body.get("data", {})
            .get("viewer", {})
            .get("accounts", [{}])[0]
            .get("r2OperationsAdaptiveGroups", [])
            or []
        )
        out: Dict[str, Dict[str, int]] = {}
        for g in groups:
            dims = g.get("dimensions") or {}
            bucket = dims.get("bucketName") or ""
            action = dims.get("actionType") or ""
            count = int((g.get("sum") or {}).get("requests") or 0)
            if not bucket or not action:
                continue
            cls = _classify_r2_op(action)
            slot = out.setdefault(
                bucket, {"classA": 0, "classB": 0, "free": 0, "unknown": 0}
            )
            if cls == "A":
                slot["classA"] += count
            elif cls == "B":
                slot["classB"] += count
            elif cls == "free":
                slot["free"] += count
            else:
                slot["unknown"] += count
        return out
    except Exception:
        log.warning("cloudflare r2 ops fetch failed", exc_info=True)
        return None


_R2_STORAGE_QUERY = """
query R2Storage($accountTag: string!, $since: Time!) {
  viewer {
    accounts(filter: {accountTag: $accountTag}) {
      r2StorageAdaptiveGroups(
        limit: 100
        filter: {datetime_geq: $since}
      ) {
        max { metadataSize objectCount payloadSize }
        dimensions { bucketName, storageClass }
      }
    }
  }
}
"""


def r2_storage_by_bucket_and_class(
    since: Optional[datetime] = None,
) -> Optional[Dict[str, Dict[str, Dict[str, int]]]]:
    """Per-bucket per-storage-class object counts + byte sizes from
    Cloudflare's authoritative metrics.

    Returns shape:
      { bucketName: { storageClass: {
          objectCount: N, payloadSize: bytes, metadataSize: bytes
      } } }

    Side-steps the S3 ``ListObjectsV2`` permission requirement — the
    Litestream credentials are correctly scoped down to put/get on
    the backups bucket only, so they can't ``list_objects_v2``. This
    GraphQL query needs only the existing Account Analytics token.

    ``since`` defaults to start-of-current-UTC-day. The dataset emits
    one row per (bucket, storage_class, datetime); we use ``max`` over
    the window because object counts are cumulative point-in-time
    samples — summing would over-count.

    Returns None if the account-analytics token isn't configured or
    the call fails. The F1 lockdown in reconcile.py treats None as
    "skip the assertion this run" rather than fail.
    """
    token = _account_analytics_token()
    acct = _account_id()
    if not token or not acct:
        return None
    if since is None:
        # 24h lookback ensures at least one CF sample is in the window
        # at any time of day (CF samples adaptively every 10-70 min;
        # using "start of today UTC" returned empty for callers that
        # ran in the first hour of a new UTC day).
        since = datetime.now(timezone.utc) - timedelta(hours=24)
    try:
        r = requests.post(
            _GRAPHQL,
            headers=_account_headers(),
            timeout=_TIMEOUT,
            json={
                "query": _R2_STORAGE_QUERY,
                "variables": {
                    "accountTag": acct,
                    "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
            },
        )
        if r.status_code != 200:
            log.warning("cloudflare r2 storage graphql returned %d", r.status_code)
            return None
        body = r.json()
        if body.get("errors"):
            log.warning("cloudflare r2 storage graphql errors: %s", body["errors"])
            return None
        groups = (
            body.get("data", {})
            .get("viewer", {})
            .get("accounts", [{}])[0]
            .get("r2StorageAdaptiveGroups", [])
            or []
        )
        out: Dict[str, Dict[str, Dict[str, int]]] = {}
        for g in groups:
            dims = g.get("dimensions") or {}
            bucket = dims.get("bucketName") or ""
            sc = dims.get("storageClass") or ""
            if not bucket or not sc:
                continue
            mx = g.get("max") or {}
            out.setdefault(bucket, {})[sc] = {
                "objectCount": int(mx.get("objectCount") or 0),
                "payloadSize": int(mx.get("payloadSize") or 0),
                "metadataSize": int(mx.get("metadataSize") or 0),
            }
        return out
    except Exception:
        log.warning("cloudflare r2 storage fetch failed", exc_info=True)
        return None


def r2_ops_reconciliation(
    ledger: Dict[str, Dict[str, Dict[str, int]]],
) -> Optional[Dict[str, Any]]:
    """Compare our ledger's per-bucket ops to Cloudflare's, surface drift.

    Takes the ``ledger`` output of ``ledger_ops_summary()`` and
    queries Cloudflare for the matching windows. Returns a dict per
    window per bucket showing CF-side totals + drift percentages,
    plus a summary list of warnings for any drift ≥ 20% (which the
    health endpoint surfaces as a status-pill flip).

    Returns None if the account-analytics token isn't configured.
    """
    token = _account_analytics_token()
    if not token:
        return None

    now = datetime.now(timezone.utc)
    day_start = now - timedelta(hours=24)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    cf_24h = r2_ops_by_bucket(day_start, now)
    cf_mtd = r2_ops_by_bucket(month_start, now)
    if cf_24h is None or cf_mtd is None:
        return None

    def _compare(window: str, ledger_counts, cf_counts) -> Dict[str, Any]:
        """Inner: build per-bucket comparison for one window."""
        out: Dict[str, Any] = {}
        # Only buckets Cloudflare actually meters (R2). The user-content
        # bucket is on B2 now, which CF can't see, so it never appears in
        # cf_counts - exclude it here (no CF side to reconcile against; its
        # ledger ops are authoritative and shown in r2Ops). Litestream
        # backups (R2) still show for an informational ledger-vs-CF view.
        all_buckets = set(cf_counts.keys())
        for bk in sorted(all_buckets):
            led = ledger_counts.get(bk, {"classA": 0, "classB": 0})
            cf = cf_counts.get(
                bk, {"classA": 0, "classB": 0, "free": 0, "unknown": 0}
            )
            drift_a_pct = _drift_pct(led["classA"], cf["classA"])
            drift_b_pct = _drift_pct(led["classB"], cf["classB"])
            out[bk] = {
                "ledgerA": led["classA"],
                "ledgerB": led["classB"],
                "cfA": cf["classA"],
                "cfB": cf["classB"],
                "cfFree": cf.get("free", 0),
                "cfUnknown": cf.get("unknown", 0),
                "driftAPct": drift_a_pct,
                "driftBPct": drift_b_pct,
            }
        return out

    cmp_24h = _compare("last24h", ledger["last24h"], cf_24h)
    cmp_mtd = _compare("monthToDate", ledger["monthToDate"], cf_mtd)

    # Drift warnings: any bucket × window × class > 20% triggers a
    # warning (status pill flips yellow on the box). Litestream
    # backups bucket is exempt from ledger-vs-CF drift since the
    # ledger never sees Litestream's ops (boto3-free), so it's
    # ALWAYS at 100% drift; the comparison there is just informational.
    warnings: list[str] = []
    # On B2 now, so it won't be in cf_counts/cmp and the drift loop below
    # finds nothing for it (correct - no CF meter to drift against).
    user_bucket = (
        os.environ.get("STORAGE_BUCKET")
        or os.environ.get("R2_BUCKET")
        or "aether-archive-tool"
    )
    for cmp_name, cmp in (("MTD", cmp_mtd), ("24h", cmp_24h)):
        user = cmp.get(user_bucket)
        if not user:
            continue
        if abs(user["driftAPct"]) >= 20 and (user["ledgerA"] + user["cfA"]) > 100:
            warnings.append(
                f"{cmp_name} Class A drift {user['driftAPct']:+.0f}% "
                f"(ledger={user['ledgerA']:,} cf={user['cfA']:,})"
            )
        if abs(user["driftBPct"]) >= 20 and (user["ledgerB"] + user["cfB"]) > 100:
            warnings.append(
                f"{cmp_name} Class B drift {user['driftBPct']:+.0f}% "
                f"(ledger={user['ledgerB']:,} cf={user['cfB']:,})"
            )

    return {
        "last24h": cmp_24h,
        "monthToDate": cmp_mtd,
        "warnings": warnings,
        "userContentBucket": user_bucket,
    }


def _drift_pct(ledger_n: int, cf_n: int) -> float:
    """Drift as percentage of Cloudflare's number (the ground truth).

    Positive = we over-counted vs CF.
    Negative = we under-counted vs CF (most common: presign abandon,
    Litestream not in our ledger).
    Zero if both sides are 0.
    Infinite when cf=0 but ledger>0 — return a large sentinel (999.0).
    """
    if ledger_n == 0 and cf_n == 0:
        return 0.0
    if cf_n == 0:
        return 999.0
    return ((ledger_n - cf_n) / cf_n) * 100.0


# --------------------------------------------------- composed health


# Free-tier ceilings (Standard storage class, per Cloudflare account
# per month). Re-declared here for the health warning thresholds —
# keeps cloudflare.py self-contained without importing from
# billing.py at module load. If billing.py's R2_CLASS_A/B_FREE_TIER_PER_MONTH
# ever change, update both.
R2_CLASS_A_FREE_TIER_MONTHLY = 1_000_000
R2_CLASS_B_FREE_TIER_MONTHLY = 10_000_000


def ledger_ops_summary() -> Dict[str, Any]:
    """Per-bucket Class A/B totals from R2OperationLog for the last 24h
    and the current calendar month, broken out by bucket.

    Pure DB query — does not hit Cloudflare. This is the "what our
    instrumentation thinks happened" side of the reconciliation
    equation. Phase E-reconcile (next sub-phase) adds the "what
    Cloudflare thinks happened" side via r2OperationsAdaptiveGroups
    and surfaces drift.

    Shape per bucket: {classA: N, classB: N}. Buckets enumerated
    explicitly so missing data shows as zero rather than a missing
    key — the UI can rely on the shape.
    """
    from datetime import timedelta
    from sqlalchemy import func
    from app.db import SessionLocal
    from app.models import R2OperationLog
    from app.ops_ledger import (
        BACKUPS_BUCKET,
        USER_CONTENT_BUCKET,
        KNOWN_BUCKETS,
    )

    now = datetime.now(timezone.utc)
    day_start = now - timedelta(hours=24)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def _zero_per_bucket() -> Dict[str, Dict[str, int]]:
        return {b: {"classA": 0, "classB": 0} for b in KNOWN_BUCKETS}

    out: Dict[str, Dict[str, Dict[str, int]]] = {
        "last24h": _zero_per_bucket(),
        "monthToDate": _zero_per_bucket(),
    }

    db = SessionLocal()
    try:
        def _fill(window_key: str, since: datetime) -> None:
            rows = (
                db.query(
                    R2OperationLog.bucket,
                    R2OperationLog.op_class,
                    func.sum(R2OperationLog.count),
                )
                .filter(R2OperationLog.day >= since)
                .group_by(R2OperationLog.bucket, R2OperationLog.op_class)
                .all()
            )
            for bucket_name, cls, total in rows:
                if bucket_name not in out[window_key]:
                    out[window_key][bucket_name] = {"classA": 0, "classB": 0}
                key = "classA" if cls == "A" else "classB" if cls == "B" else None
                if key is not None:
                    out[window_key][bucket_name][key] = int(total or 0)

        _fill("last24h", day_start)
        _fill("monthToDate", month_start)
    finally:
        db.close()

    # Annotate which buckets are user-attributable vs platform-fixed.
    # The user-content bucket holds ops driven by real user activity;
    # the backups bucket is Litestream + cron only.
    out["userContentBucket"] = USER_CONTENT_BUCKET
    out["backupsBucket"] = BACKUPS_BUCKET
    return out


def cloudflare_health() -> Dict[str, Any]:
    """One-stop shape consumed by /api/admin/service-health.

    Returns a status pill ('active' | 'warning' | 'down') plus the
    live numbers. When CLOUDFLARE_API_TOKEN isn't set we return
    {status: 'active', configured: False} — Cloudflare itself is
    fine, we just have no visibility into it.

    Note: the ``r2Ops`` block is always populated (it's a DB query
    against R2OperationLog and doesn't require the Cloudflare token).
    The Cloudflare-side counters (dns, email, bandwidth) are only
    populated when the token is configured.

    Warning conditions (any of these flips status to 'warning'):
      - DNS records ≥ 75% of cap (150 of 200)
      - Email Routing rules ≥ 75% of cap (150 of 200)
      - Class A ops in current month ≥ 75% of 1M free tier
      - Class B ops in current month ≥ 75% of 10M free tier
    """
    # R2 ops summary is always available regardless of CF token state.
    r2_ops = ledger_ops_summary()

    # Reconciliation against Cloudflare's authoritative per-bucket
    # totals. None when the account-analytics token isn't configured;
    # the box renders ledger-only in that case.
    r2_recon = r2_ops_reconciliation(r2_ops)

    # Sum across all buckets for the free-tier caps (Cloudflare's free
    # tier is account-wide, not per-bucket).
    total_a_mtd = sum(b["classA"] for b in r2_ops["monthToDate"].values())
    total_b_mtd = sum(b["classB"] for b in r2_ops["monthToDate"].values())

    if not _token() or not _zone_id():
        warnings = []
        if total_a_mtd >= int(R2_CLASS_A_FREE_TIER_MONTHLY * 0.75):
            warnings.append(
                f"Class A ops at {total_a_mtd:,}/{R2_CLASS_A_FREE_TIER_MONTHLY:,}/mo (≥75%)"
            )
        if total_b_mtd >= int(R2_CLASS_B_FREE_TIER_MONTHLY * 0.75):
            warnings.append(
                f"Class B ops at {total_b_mtd:,}/{R2_CLASS_B_FREE_TIER_MONTHLY:,}/mo (≥75%)"
            )
        if r2_recon and r2_recon["warnings"]:
            warnings.extend(r2_recon["warnings"])
        return {
            "status": "warning" if warnings else "active",
            "configured": False,
            "warnings": warnings,
            "r2Ops": r2_ops,
            "r2OpsReconciliation": r2_recon,
            "r2OpsClassAFreeTier": R2_CLASS_A_FREE_TIER_MONTHLY,
            "r2OpsClassBFreeTier": R2_CLASS_B_FREE_TIER_MONTHLY,
        }

    dns_count = dns_record_count()
    email_rules = email_routing_rule_count()
    day = bandwidth_24h()
    three_d = bandwidth_last_3d()

    # Warning thresholds.
    warnings = []
    if dns_count is not None and dns_count >= int(DNS_RECORDS_CAP * 0.75):
        warnings.append(
            f"DNS records at {dns_count}/{DNS_RECORDS_CAP} (>=75%)"
        )
    if (
        email_rules is not None
        and email_rules >= int(EMAIL_ROUTING_RULES_CAP * 0.75)
    ):
        warnings.append(
            f"Email Routing rules at {email_rules}/{EMAIL_ROUTING_RULES_CAP} (>=75%)"
        )
    if total_a_mtd >= int(R2_CLASS_A_FREE_TIER_MONTHLY * 0.75):
        warnings.append(
            f"Class A ops at {total_a_mtd:,}/{R2_CLASS_A_FREE_TIER_MONTHLY:,}/mo (≥75%)"
        )
    if total_b_mtd >= int(R2_CLASS_B_FREE_TIER_MONTHLY * 0.75):
        warnings.append(
            f"Class B ops at {total_b_mtd:,}/{R2_CLASS_B_FREE_TIER_MONTHLY:,}/mo (≥75%)"
        )
    if r2_recon and r2_recon["warnings"]:
        warnings.extend(r2_recon["warnings"])

    status = "warning" if warnings else "active"

    return {
        "status": status,
        "configured": True,
        "warnings": warnings,
        "dnsRecordCount": dns_count,
        "dnsRecordsCap": DNS_RECORDS_CAP,
        "emailRoutingRuleCount": email_rules,
        "emailRoutingRulesCap": EMAIL_ROUTING_RULES_CAP,
        "last24h": day,  # {bytes, cachedBytes, requests, cachedRequests, threats}
        "last3d": three_d,
        "r2Ops": r2_ops,
        "r2OpsReconciliation": r2_recon,
        "r2OpsClassAFreeTier": R2_CLASS_A_FREE_TIER_MONTHLY,
        "r2OpsClassBFreeTier": R2_CLASS_B_FREE_TIER_MONTHLY,
        "r2BillingSummary": r2_billing_summary(),
        # Per-bucket point-in-time bytes-on-disk + object counts, keyed by
        # bucket name; each value is `{storageClass: {payloadSize,
        # metadataSize, objectCount}}`. The user-content bucket is read from
        # B2 (cached S3 list); any R2 buckets (Litestream backups) come from
        # Cloudflare GraphQL. Feeds the Stack tab's storage card.
        "r2StorageSnapshot": _storage_snapshot(),
    }


# ------------------------- bill reconciliation -----------------------


# Cached B2 storage walk. The user-content bucket lives on Backblaze B2 since
# the storage migration, so Cloudflare's R2 GraphQL can't see it; we list it
# directly via the S3 API (r2.bucket_stats). Cache ~5 min so admin-health polls
# don't each issue a LIST (one Class A op per page).
_b2_storage_cache: Dict[str, Any] = {"at": 0.0, "stats": None}


def _b2_user_storage() -> Optional[Dict[str, int]]:
    """Current ``{objects, bytes}`` for the user-content bucket, read from B2.

    Returns None if object storage isn't configured. Cached briefly so a burst
    of admin-health polls doesn't list the bucket on every call.
    """
    import time  # noqa: WPS433

    now = time.monotonic()
    cached = _b2_storage_cache["stats"]
    if cached is not None and now - _b2_storage_cache["at"] < 300:
        return cached
    from app import ops_ledger, r2  # noqa: WPS433 (lazy, avoid import cycle)

    stats = r2.bucket_stats(subject=ops_ledger.PLATFORM)
    if stats is not None:
        _b2_storage_cache["at"] = now
        _b2_storage_cache["stats"] = stats
    return stats


def _storage_snapshot() -> Optional[Dict[str, Any]]:
    """Per-bucket point-in-time storage for the Stack tab's storage card.

    The user-content bucket is on B2 (listed via S3, surfaced as a single
    synthetic 'Standard' class since B2 has no per-class split); any remaining
    R2 buckets (Litestream backups) still come from Cloudflare's GraphQL.
    """
    snap = r2_storage_by_bucket_and_class() or {}
    stats = _b2_user_storage()
    if stats is not None:
        bucket = (
            os.environ.get("STORAGE_BUCKET")
            or os.environ.get("R2_BUCKET")
            or "aether-archive-tool"
        )
        snap[bucket] = {
            "Standard": {
                "objectCount": stats["objects"],
                "payloadSize": stats["bytes"],
                "metadataSize": 0,
            }
        }
    return snap or None


def r2_billing_summary() -> Optional[Dict[str, Any]]:
    """Live snapshot reconciliation: our ledger vs the store's actual bytes.

    Pulls the CURRENT bytes-on-disk straight from the object store + sums our
    active StorageObject rows. For write-once archival data these should match
    within a few percent at any moment. Multiplies by hours-in-period for an
    approximate month-to-date cost estimate on each side, then surfaces the gap.

    The user-content bucket is on Backblaze B2 now, which Cloudflare's R2
    GraphQL can't see, so we list B2 directly (cached). The returned ``cf*``
    fields keep their names for response-shape stability but mean "the object
    store's actual numbers" (B2). Returns None when storage isn't configured.

    Storage only - per-op reconciliation lives in r2_ops_reconciliation
    and surfaces separately as detail rows below the box's metrics.
    """
    from app import billing as billing_lib  # noqa: WPS433
    from app.db import SessionLocal  # noqa: WPS433
    from app.models import ReconciliationLog, StorageObject  # noqa: WPS433

    user_bucket = (
        os.environ.get("STORAGE_BUCKET")
        or os.environ.get("R2_BUCKET")
        or "aether-archive-tool"
    )
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    period_hours = (now - month_start).total_seconds() / 3600.0

    # Actual bytes-on-disk in the user-content bucket, read from B2 (cached).
    stats = _b2_user_storage()
    if stats is None:
        return None
    cf_bytes = stats["bytes"]

    # Our ledger snapshot + latest cron-run row.
    db = SessionLocal()
    try:
        ours_bytes = sum(
            (r.bytes + r.metadata_bytes)
            for r in db.query(StorageObject)
            .filter(StorageObject.deleted_at.is_(None))
            .all()
        )
        last_run_row = (
            db.query(ReconciliationLog)
            .filter(ReconciliationLog.action == "r2_billing_drift_storage")
            .order_by(ReconciliationLog.ran_at.desc())
            .first()
        )
    finally:
        db.close()

    cf_cost = cf_bytes * period_hours * billing_lib.STORAGE_COST_USD_PER_BYTE_HOUR
    ours_cost = ours_bytes * period_hours * billing_lib.STORAGE_COST_USD_PER_BYTE_HOUR

    if cf_bytes > 0:
        drift_pct = ((ours_bytes - cf_bytes) / cf_bytes) * 100.0
    else:
        drift_pct = 0.0 if ours_bytes == 0 else 999.0

    last_cron_run: Optional[Dict[str, Any]] = None
    if last_run_row is not None:
        ran_at = last_run_row.ran_at
        if ran_at.tzinfo is None:
            ran_at = ran_at.replace(tzinfo=timezone.utc)
        try:
            details = json.loads(last_run_row.details_json or "{}")
        except json.JSONDecodeError:
            details = {}
        last_cron_run = {
            "ranAt": ran_at.isoformat(),
            "periodLabel": details.get("label"),
            "driftPct": details.get("drift_pct"),
            "driftUsd": details.get("drift_usd"),
            "alerted": bool(last_run_row.alerted),
        }

    return {
        "periodStart": month_start.isoformat(),
        "periodEnd": now.isoformat(),
        "userBucket": user_bucket,
        "oursBytes": ours_bytes,
        "cfBytes": cf_bytes,
        "oursCostUsd": ours_cost,
        "cfCostUsd": cf_cost,
        "driftUsd": ours_cost - cf_cost,
        "driftPct": drift_pct,
        "lastCronRun": last_cron_run,
    }
