"""Daily storage metering — write one UsageRecord per active user per day.

Run from a systemd timer once a day (anytime is fine — we use UTC midnight
as the bucket key so re-running the same day is idempotent).

Algorithm:
  For each user:
    bytes_stored = SUM(fileSizeBytes) over their UserChannelVideo rows
    Upsert UsageRecord(user_id, day=today_utc_00:00, bytes_stored).

We store the raw byte count, not the dollar amount. That way pricing
changes don't require recomputing history.

Usage:
    /opt/aether/venv/bin/python -m scripts.meter
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from app.db import SessionLocal
from app.models import UsageRecord, User, UserChannel, UserChannelVideo


log = logging.getLogger("aether.meter")


def _today_utc_midnight() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def main() -> int:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )

    today = _today_utc_midnight()
    written = 0
    skipped = 0

    db = SessionLocal()
    try:
        users = db.query(User).all()
        log.info("metering %d users for day=%s", len(users), today.date().isoformat())

        for user in users:
            # Sum stored bytes from the user's videos. fileSizeBytes lives in
            # the JSON blob — we only count rows that have actually been
            # archived (others have no file in R2 yet). Videos belonging
            # to soft-deleted channels (UserChannel.removed_at IS NOT NULL)
            # are excluded - we promised the user we'd stop charging
            # them during the grace window even though the bytes are
            # still on disk.
            active_channel_ids = {
                cid
                for (cid,) in (
                    db.query(UserChannel.channel_id)
                    .filter(
                        UserChannel.user_id == user.id,
                        UserChannel.removed_at.is_(None),
                    )
                    .all()
                )
            }
            rows = (
                db.query(UserChannelVideo)
                .filter(
                    UserChannelVideo.user_id == user.id,
                    UserChannelVideo.channel_id.in_(active_channel_ids),
                )
                .all()
                if active_channel_ids
                else []
            )
            total_bytes = 0
            for r in rows:
                try:
                    data = json.loads(r.data_json)
                except json.JSONDecodeError:
                    continue
                n = data.get("fileSizeBytes")
                if isinstance(n, int) and n > 0:
                    total_bytes += n

            if total_bytes == 0:
                # No archived bytes today — still write a zero row? We skip,
                # because UsageRecord rows only matter when they accumulate
                # cost. Saves DB rows for users who never sync anything.
                skipped += 1
                continue

            # Upsert: composite key isn't declared, so query then update/insert.
            existing = (
                db.query(UsageRecord)
                .filter(UsageRecord.user_id == user.id, UsageRecord.day == today)
                .first()
            )
            if existing is not None:
                # Re-running the cron same day — overwrite with latest count.
                existing.bytes_stored = total_bytes
                # If somehow it was already billed, leave it billed — this is
                # only fresh data for an unbilled day in the normal flow.
            else:
                rec = UsageRecord(
                    user_id=user.id,
                    day=today,
                    bytes_stored=total_bytes,
                    billed=False,
                )
                db.add(rec)
            written += 1

        db.commit()
        log.info("meter complete: wrote=%d skipped=%d", written, skipped)
        return 0
    except Exception:
        log.exception("meter run failed")
        db.rollback()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
