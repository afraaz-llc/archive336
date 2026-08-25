"""Daily Sentry quota check — email the admin when events cross threshold.

Independent tripwire for the Sentry monthly free-tier event count.
Runs once per day via the archive336-sentry-quota.timer systemd unit,
so the operator gets alerted even if nobody's looking at the admin
panel. Threshold + email plumbing live in app.alerts and app.email
respectively; this script is just the cron-side driver.

Behavior:
  - Polls Sentry's stats_v2 endpoint for the current month's error
    event count using SENTRY_AUTH_TOKEN.
  - If events_this_month >= alerts.SENTRY_WARNING_THRESHOLD and we
    haven't already alerted this month (file-marker dedup), sends
    the admin an email via app.email.send_sentry_quota_warning.
  - Logs every run to stderr — the systemd journal is the audit
    trail.

Exit codes:
  0  - ran cleanly (alert fired OR threshold not crossed OR already
       alerted this month)
  1  - configuration missing (no SENTRY_AUTH_TOKEN) or Sentry API
       call failed. Timer re-fires tomorrow.

Usage:
    /opt/aether/venv/bin/python -m scripts.check_sentry_quota
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone


log = logging.getLogger("aether.check_sentry_quota")


SENTRY_MONTHLY_CAP = 5000
SENTRY_ORG_SLUG = "archive336"


def _fetch_events_this_month(token: str) -> int:
    """Hit Sentry's stats_v2 endpoint and sum month-to-date error
    events. Raises on any HTTP / parse failure."""
    import requests

    now = datetime.now(timezone.utc)
    month_start = now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    resp = requests.get(
        f"https://sentry.io/api/0/organizations/{SENTRY_ORG_SLUG}/stats_v2/",
        params={
            "field": "sum(quantity)",
            "category": "error",
            "start": month_start.isoformat().replace("+00:00", "Z"),
            "end": now.isoformat().replace("+00:00", "Z"),
            "interval": "1d",
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    total = 0
    for group in data.get("groups") or []:
        total += int(group.get("totals", {}).get("sum(quantity)") or 0)
    return total


def main() -> int:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )

    from app import alerts

    token = os.environ.get("SENTRY_AUTH_TOKEN")
    if not token:
        log.error(
            "SENTRY_AUTH_TOKEN missing from env; cannot poll Sentry. "
            "Set it in /opt/aether/.env and retry."
        )
        return 1

    try:
        events = _fetch_events_this_month(token)
    except Exception:
        log.exception("sentry stats fetch failed")
        return 1

    log.info(
        "sentry events this month: %d / %d (threshold %d)",
        events,
        SENTRY_MONTHLY_CAP,
        alerts.SENTRY_WARNING_THRESHOLD,
    )

    if events < alerts.SENTRY_WARNING_THRESHOLD:
        log.info("under threshold; nothing to alert")
        return 0

    sent = alerts.maybe_send_sentry_quota_alert(events, SENTRY_MONTHLY_CAP)
    if sent:
        log.info("alert email sent")
    else:
        log.info("alert NOT sent (already alerted this month, or send failed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
