"""Operational alert helpers — email-the-admin tripwires.

Centralized home for "noticed something noteworthy, email the operator
about it" plumbing. Right now there's one alert (Sentry quota); future
quota tripwires (Resend, Hetzner egress, etc.) should follow the same
pattern: a small public function per alert type, file-marker dedup,
graceful no-op when env config is missing.

Dedup is intentionally file-marker based (not DB-backed): the cron
that drives these alerts runs once per day on a single Hetzner box,
so an mtime-on-disk marker is the simplest reliable thing. Markers
roll over naturally each calendar month — last month's marker is
just irrelevant.

The alert send target is read from ADMIN_ALERT_EMAIL (env), with a
hardcoded fallback to the project owner's address so the feature
works out of the box on the prod box.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone


log = logging.getLogger("archive336.alerts")


# Where dedup marker files live. Created on first send if missing.
# Backed by /opt/aether/state/ on the prod box; archive336-api + cron both
# run as root (per the systemd units), so write perms are uniform.
ALERT_STATE_DIR = "/opt/aether/state"


# Sentry's free tier is 5,000 errors/mo. We alert at half of that so
# there's time to react (upgrade plan, fix the noisy code path, etc.)
# before Sentry silently drops further events.
SENTRY_WARNING_THRESHOLD = 2500


# Hetzner's CPX21 plan includes 2 TB outbound per billing period;
# overage is ~$1.07/TB. Alert when we've burned half the included
# traffic — small per-TB cost makes catastrophe unlikely, but we
# still want a heads-up so a leak or hot user doesn't quietly chew
# through the cap.
HETZNER_BANDWIDTH_WARNING_PCT = 50.0


def _admin_email() -> str:
    """Recipient for operational alerts. Env override, hardcoded
    fallback so the feature works without setup on the prod box."""
    return os.environ.get("ADMIN_ALERT_EMAIL", "")


def maybe_send_sentry_quota_alert(events: int, cap: int) -> bool:
    """Send the Sentry quota email if we haven't already this month.

    Returns True if an email was sent on this call, False otherwise
    (already sent this month, send failed, etc.). Caller decides
    whether to act on the return value — the polling UI path ignores
    it, the cron script logs it.
    """
    now = datetime.now(timezone.utc)
    marker = os.path.join(
        ALERT_STATE_DIR, f"sentry-quota-{now.year}-{now.month:02d}"
    )
    if os.path.exists(marker):
        return False
    to_email = _admin_email()
    if not to_email:
        log.warning("ADMIN_ALERT_EMAIL unset; alert not sent")
        return False
    try:
        from app.email import send_sentry_quota_warning

        send_sentry_quota_warning(to_email, events, cap)
    except Exception:
        log.exception("sentry quota alert email failed")
        return False

    # Write marker AFTER the email actually went out. O_EXCL so two
    # simultaneous callers can't both think they were first (matters
    # if the polling UI path ever re-enters this in parallel with the
    # cron).
    try:
        os.makedirs(ALERT_STATE_DIR, exist_ok=True)
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{events}/{cap} at {now.isoformat()}\n".encode())
        os.close(fd)
    except FileExistsError:
        # Another caller beat us to the marker write. Email already
        # went out from us though, so this is fine - the next call
        # will short-circuit on the marker.
        pass
    except Exception:
        log.exception("sentry quota marker write failed")
    return True


def maybe_send_hetzner_bandwidth_alert(
    used_bytes: int, included_bytes: int
) -> bool:
    """Send the Hetzner bandwidth email if we haven't already this month.

    File-marker dedup, same shape as the Sentry alert. Caller is the
    daily cron in scripts/check_hetzner_bandwidth.py; not currently
    wired into the live UI path (the Stack tab's Hetzner pill is
    advisory only). Calendar-month markers don't perfectly align with
    Hetzner's billing-period reset, so in the worst case (we're
    persistently >50% during a billing-period boundary that straddles
    a calendar-month boundary) we might fire one extra email. Worth
    the simplicity at this scale.
    """
    now = datetime.now(timezone.utc)
    marker = os.path.join(
        ALERT_STATE_DIR, f"hetzner-bandwidth-{now.year}-{now.month:02d}"
    )
    if os.path.exists(marker):
        return False
    to_email = _admin_email()
    if not to_email:
        log.warning("ADMIN_ALERT_EMAIL unset; alert not sent")
        return False
    try:
        from app.email import send_hetzner_bandwidth_warning

        send_hetzner_bandwidth_warning(to_email, used_bytes, included_bytes)
    except Exception:
        log.exception("hetzner bandwidth alert email failed")
        return False

    try:
        os.makedirs(ALERT_STATE_DIR, exist_ok=True)
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        pct = (used_bytes / included_bytes * 100) if included_bytes else 0
        os.write(
            fd,
            f"{used_bytes}/{included_bytes} ({pct:.1f}%) at {now.isoformat()}\n".encode(),
        )
        os.close(fd)
    except FileExistsError:
        pass
    except Exception:
        log.exception("hetzner bandwidth marker write failed")
    return True


# A stalled queue is re-alertable, unlike a monthly quota: the same
# outage on two consecutive days is two things the operator needs to
# know about, not one. Marker rolls over daily rather than monthly.
def maybe_send_queue_stalled_alert(summary: str) -> bool:
    """Email the operator that somebody's backup has stopped moving.

    Once per calendar day. The condition this reports - queued work, a
    live worker, nothing completing - is exactly the shape of the
    storage-cap outage that ran for hours in production with nothing
    anywhere saying so.
    """
    to_email = _admin_email()
    if not to_email:
        log.warning("ADMIN_ALERT_EMAIL unset; alert not sent")
        return False
    now = datetime.now(timezone.utc)
    marker = os.path.join(
        ALERT_STATE_DIR, f"queue-stalled-{now:%Y-%m-%d}.sent"
    )
    if os.path.exists(marker):
        return False

    try:
        from app.email import send_queue_stalled_warning

        send_queue_stalled_warning(to_email, summary)
    except Exception:
        log.exception("queue stalled alert email failed")
        return False

    try:
        os.makedirs(ALERT_STATE_DIR, exist_ok=True)
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{now.isoformat()}\n{summary}\n".encode())
        os.close(fd)
    except FileExistsError:
        pass
    except Exception:
        log.exception("queue stalled marker write failed")
    return True
