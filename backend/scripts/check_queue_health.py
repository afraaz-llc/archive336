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
        lines.append(f'{f.count} failures in the last hour: "{f.error}"')

    summary = "\n".join(lines)
    log.warning("queue health: %s", summary.replace("\n", " | "))

    if alerts.maybe_send_queue_stalled_alert(summary):
        log.info("operator alerted")
    else:
        log.info("already alerted today; not sending again")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
