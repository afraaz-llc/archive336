"""Hourly check: is anybody's backup silently stopped?

Runs from the archive336-queue-health.timer systemd unit. The condition
it reports for - queued work, a live worker, and nothing completing -
is exactly the shape of the storage-cap outage that ran for hours in
production while every surface in the product said things were fine.

Detection lives in app.queue_health, the email in app.email, the
once-a-day dedup in app.alerts. This script is only the cron driver.

Exit codes:
  0 - ran cleanly (alert fired, or nothing wrong, or already alerted)
  1 - could not run (database unreachable, etc.)
"""
from __future__ import annotations

import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("check_queue_health")


def main() -> int:
    try:
        from app.db import SessionLocal
        from app import alerts, queue_health
    except Exception:
        log.exception("import failed")
        return 1

    try:
        db = SessionLocal()
    except Exception:
        log.exception("database unreachable")
        return 1

    try:
        stalled = queue_health.find_stalled_users(db)
        storms = queue_health.find_failure_storms(db)
    except Exception:
        log.exception("queue health check failed")
        return 1
    finally:
        db.close()

    if not stalled and not storms:
        log.info("queues healthy: nothing stalled, no failure storms")
        return 0

    lines = []
    for s in stalled:
        last = s.last_done_at.isoformat() if s.last_done_at else "never"
        lines.append(
            f"{s.username}: {s.pending} job(s) queued, last completion {last}"
        )
    for f in storms:
        lines.append(
            f'{f.count} {f.kind} failures in the last hour: "{f.error}"'
        )

    # Name the condition that actually happened.
    #
    # This used to say "A backup queue has stalled" whatever it found,
    # and sent that headline for a night when no backup had stalled and
    # no video job had failed at all - every failure was the comment
    # rescan meeting a stale YouTube session. An operator who learns
    # that the headline is unreliable stops reading the alert, which
    # costs more than the false alarm did.
    video_storms = [f for f in storms if f.kind in (None, "video")]
    if stalled:
        headline = "A backup queue has stalled"
    elif video_storms:
        headline = "Backups are failing repeatedly"
    elif storms:
        kinds = sorted({f.kind for f in storms})
        headline = f"{' and '.join(kinds).capitalize()} jobs are failing repeatedly"
    else:  # pragma: no cover - guarded by the early return above
        headline = "A backup queue has stalled"

    summary = "\n".join(lines)
    log.warning("queue health: %s", summary.replace("\n", " | "))

    if alerts.maybe_send_queue_stalled_alert(summary, headline=headline):
        log.info("operator alerted")
    else:
        log.info("already alerted today; not sending again")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
