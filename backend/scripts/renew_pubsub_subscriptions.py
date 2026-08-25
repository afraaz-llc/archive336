"""Daily PubSub renewal cron.

YouTube's PubSubHubbub hub gives 10-day leases. Without renewal, our
subscriptions expire and we stop receiving upload notifications for
those channels. This script re-subscribes any Channel whose
pubsub_lease_expires_at is within the next 2 days, plus any Channel
that doesn't have a subscription yet (catches anything that was
created before PubSub was wired in or that failed its initial
subscribe).

Channels with zero active subscribers (no UserChannelSubscription with
unsubscribed_at IS NULL) are skipped: the hub subscription is shared
across subscribers, so once the last one leaves there's no one to
notify. Skipping lets the lease lapse naturally instead of renewing it
forever for a channel nobody tracks.

Runs daily at 03:30 UTC via the archive336-pubsub-renew.timer systemd
unit. Persistent=true so a missed run catches up on next boot.

Exit codes:
  0  - ran cleanly
  1  - PUBSUB_SECRET / BASE_URL config missing, or the hub flat-out
       refused every attempt. Timer retries tomorrow.

Usage:
    /opt/aether/venv/bin/python -m scripts.renew_pubsub_subscriptions --dry-run
    /opt/aether/venv/bin/python -m scripts.renew_pubsub_subscriptions
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, or_

from app import pubsub
from app.db import SessionLocal
from app.models import Channel, UserChannelSubscription


log = logging.getLogger("aether.renew_pubsub")


# Renew anything that'll lapse within this many days. Hub leases are
# 10 days, so 2 days of buffer = 8 days between renewals on average.
RENEW_WITHIN_DAYS = 2


def channels_needing_renewal(db, now: datetime) -> list[Channel]:
    """Channels whose PubSub lease should be (re)subscribed at ``now``.

    A channel qualifies when it has at least one active subscriber AND
    either has no lease yet or one expiring within RENEW_WITHIN_DAYS.
    Zero-subscriber channels are excluded on purpose so their lease
    lapses instead of being renewed forever (see module docstring).
    """
    renew_cutoff = now + timedelta(days=RENEW_WITHIN_DAYS)
    has_active_subscriber = exists().where(
        UserChannelSubscription.channel_id == Channel.id,
        UserChannelSubscription.unsubscribed_at.is_(None),
    )
    return (
        db.query(Channel)
        .filter(
            or_(
                Channel.pubsub_lease_expires_at.is_(None),
                Channel.pubsub_lease_expires_at <= renew_cutoff,
            ),
            has_active_subscriber,
        )
        .all()
    )


def main(argv: list[str]) -> int:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )
    dry_run = "--dry-run" in argv

    now = datetime.now(timezone.utc)

    db = SessionLocal()
    try:
        rows = channels_needing_renewal(db, now)
        log.info("found %d channels needing pubsub renewal", len(rows))

        ok_count = 0
        fail_count = 0
        for ch in rows:
            log.info(
                "  %s (lease=%s): subscribing",
                ch.youtube_id,
                ch.pubsub_lease_expires_at,
            )
            if dry_run:
                continue
            try:
                if pubsub.subscribe_channel(ch.youtube_id):
                    ch.pubsub_last_renewed_at = now
                    ch.pubsub_lease_expires_at = now + timedelta(days=10)
                    ok_count += 1
                else:
                    fail_count += 1
            except RuntimeError as exc:
                # BASE_URL / PUBSUB_SECRET missing - whole run is
                # broken at this point, just bail.
                log.error("pubsub config broken: %s", exc)
                return 1
            except Exception:
                log.exception(
                    "pubsub subscribe raised for %s", ch.youtube_id
                )
                fail_count += 1

        if dry_run:
            log.info("dry-run: rolling back")
            db.rollback()
        else:
            db.commit()
            log.info("renewed=%d failed=%d", ok_count, fail_count)
        return 0
    except Exception:
        log.exception("renewal run failed")
        db.rollback()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
