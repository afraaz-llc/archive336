"""Daily purge - hard-delete soft-deleted channels past the grace window.

Run from a systemd timer once a day. For each UserChannel where
removed_at is older than SOFT_DELETE_GRACE_DAYS:
  1. Collect every R2 key tied to the channel (thumbnails on its
     videos + the channel avatar + the archived .mp4 files via the
     localPath field on each video's data_json).
  2. r2.delete_keys() in batches.
  3. Delete UserChannelVideo rows for the channel.
  4. Delete the UserChannel row itself.

All commits are per-channel so a partial failure doesn't roll back
prior successes. R2 deletions are best-effort (logged but not fatal)
because we'd rather the DB row be gone than orphan + a half-deleted
channel.

Usage:
    /opt/aether/venv/bin/python -m scripts.purge_removed        # real
    /opt/aether/venv/bin/python -m scripts.purge_removed --dry  # report only
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import List

from sqlalchemy import or_

from app import r2, storage_ledger
from app.db import SessionLocal
from app.models import (
    Channel,
    ChannelOwnership,
    ErrorLog,
    UserChannel,
    UserChannelSubscription,
    UserChannelVideo,
    UserSession,
    Video,
)


# Privacy policy commitment: request logs + session IP/user-agent are kept
# for at most 30 days (see Privacy.tsx "Server logs").
LOG_RETENTION_DAYS = 30


log = logging.getLogger("aether.purge_removed")


# Keep in lock-step with SOFT_DELETE_GRACE_DAYS in routes/youtube.py.
# If we ever want different windows for different scopes (e.g. videos
# vs accounts) split into separate constants there.
GRACE_DAYS = 30


def _r2_keys_for_channel(
    db: SessionLocal, user_id: str, channel_id: str
) -> List[str]:
    """Every storage key to drop for this channel.

    Delegates to storage_ledger.channel_r2_keys - the same function the
    soft-delete uses to MARK these objects. This used to be a second copy
    whose docstring claimed the two mirrored each other; they drifted. This
    copy only matched the legacy "videos/..." prefix, so after storage moved
    to per-user keys (users/<uid>/videos/...) purge deleted thumbnails and
    the avatar but silently skipped every video file - the largest objects -
    while the ledger marked them deleted and the UI said the archive was
    erased. Delegating means the set that gets marked is exactly the set
    that gets erased.
    """
    return storage_ledger.channel_r2_keys(db, user_id, channel_id)


def _aware(ts):
    """Treat a naive timestamp as UTC. SQLite hands these back naive."""
    if ts is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def purge_channel(db, user_id: str, channel_id: str) -> int:
    """Hard-delete ONE soft-deleted channel NOW: its R2 objects, its
    UserChannelVideo rows, then the UserChannel row. Returns the number of
    R2 keys targeted. Commits. Shared by the daily cron and the on-demand
    "Delete permanently" endpoint so both do exactly the same thing. R2
    deletion is best-effort (logged, not fatal) — we'd rather the DB row be
    gone than leave a half-deleted orphan.
    """
    keys = _r2_keys_for_channel(db, user_id, channel_id)
    if keys:
        try:
            r2.delete_keys(keys)
            storage_ledger.mark_deleted(db, keys)
        except Exception:  # noqa: BLE001
            log.exception(
                "r2 cleanup failed for user %s channel %s; continuing",
                user_id,
                channel_id,
            )
    db.query(UserChannelVideo).filter(
        UserChannelVideo.user_id == user_id,
        UserChannelVideo.channel_id == channel_id,
    ).delete(synchronize_session=False)
    ch = db.get(UserChannel, (user_id, channel_id))
    if ch is not None:
        db.delete(ch)
    db.commit()
    return len(keys)


def purge_orphaned_pool_channels(db, *, dry_run: bool = False) -> dict:
    """Delete shared-pool Channel + Video rows nobody subscribes to.

    The pool holds ONE Channel row and one Video row per real video,
    shared by everyone who tracks that channel - which is why deleting a
    channel only ever soft-deleted the caller's subscription: someone
    else might still be using the rows.

    Nothing ever deleted them when the last person left. purge_channel()
    above removes R2 objects and the LEGACY UserChannel /
    UserChannelVideo rows, and stops there, so the shared catalogue -
    including the titles of private videos - stayed in the database
    forever for channels nobody tracks any more. A user who deleted a
    channel and waited out the grace still had their private video
    titles on our disk, and re-adding the channel repopulated their
    screen from them.

    A channel is orphaned when every subscription to it is unsubscribed
    and past the same grace window the rest of the purge uses, so a user
    who restores inside 30 days still gets their catalogue back.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=GRACE_DAYS)
    out = {"channels": 0, "videos": 0}

    for channel in db.query(Channel).all():
        subs = (
            db.query(UserChannelSubscription)
            .filter(UserChannelSubscription.channel_id == channel.id)
            .all()
        )
        # Never touch a channel somebody still tracks, or one still
        # inside a restorable grace window.
        if any(
            s.unsubscribed_at is None
            or _aware(s.unsubscribed_at) >= cutoff
            for s in subs
        ):
            continue
        # A channel with no subscriptions at all is only orphaned if it
        # is not brand new - otherwise we would delete a channel mid-add,
        # between ensure_channel() and ensure_subscription().
        if not subs and _aware(channel.created_at) >= cutoff:
            continue

        videos = db.query(Video).filter(Video.channel_id == channel.id).all()
        out["channels"] += 1
        out["videos"] += len(videos)
        if dry_run:
            continue
        for v in videos:
            db.delete(v)
        for s in subs:
            db.delete(s)
        db.query(ChannelOwnership).filter(
            ChannelOwnership.channel_id == channel.id
        ).delete(synchronize_session=False)
        db.delete(channel)
        db.commit()

    return out


def purge_old_logs(db, *, dry_run: bool = False) -> dict:
    """Enforce the privacy policy's <=30-day retention for request logs and
    session PII:
      - delete ErrorLog rows (the request-log records) past the window,
      - delete expired sessions past the window,
      - and on still-active old logins, drop the IP + user-agent while
        leaving the session itself intact (don't log people out).
    Returns counts. Commits unless dry_run.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=LOG_RETENTION_DAYS)
    counts = {"error_logs": 0, "sessions_deleted": 0, "sessions_scrubbed": 0}

    old_errors = db.query(ErrorLog).filter(ErrorLog.created_at < cutoff)
    counts["error_logs"] = old_errors.count()

    expired_old = db.query(UserSession).filter(
        UserSession.created_at < cutoff,
        UserSession.expires_at < now,
    )
    counts["sessions_deleted"] = expired_old.count()

    active_old = db.query(UserSession).filter(
        UserSession.created_at < cutoff,
        UserSession.expires_at >= now,
        or_(
            UserSession.ip_address.is_not(None),
            UserSession.user_agent.is_not(None),
        ),
    )
    counts["sessions_scrubbed"] = active_old.count()

    if not dry_run:
        old_errors.delete(synchronize_session=False)
        expired_old.delete(synchronize_session=False)
        active_old.update(
            {UserSession.ip_address: None, UserSession.user_agent: None},
            synchronize_session=False,
        )
        db.commit()
    return counts


def main(argv: list[str]) -> int:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )
    dry_run = "--dry" in argv

    cutoff = datetime.now(timezone.utc) - timedelta(days=GRACE_DAYS)
    purged = 0
    skipped = 0

    db = SessionLocal()
    try:
        rows = (
            db.query(UserChannel)
            .filter(
                UserChannel.removed_at.is_not(None),
                UserChannel.removed_at < cutoff,
            )
            .all()
        )
        log.info(
            "purge run starting (dry=%s) over %d channels past %d-day grace",
            dry_run,
            len(rows),
            GRACE_DAYS,
        )

        for ch in rows:
            if dry_run:
                keys = _r2_keys_for_channel(db, ch.user_id, ch.channel_id)
                log.info(
                    "user %s channel %s: removed_at=%s, %d r2 keys (dry)",
                    ch.user_id,
                    ch.channel_id,
                    ch.removed_at.isoformat() if ch.removed_at else "?",
                    len(keys),
                )
                skipped += 1
                continue
            n = purge_channel(db, ch.user_id, ch.channel_id)
            log.info(
                "user %s channel %s purged (%d r2 keys)",
                ch.user_id,
                ch.channel_id,
                n,
            )
            purged += 1

        # Shared-pool cleanup: drop catalogues nobody tracks any more.
        orphans = purge_orphaned_pool_channels(db, dry_run=dry_run)
        log.info(
            "orphaned pool channels: channels=%d videos=%d",
            orphans["channels"],
            orphans["videos"],
        )

        # Enforce log/PII retention alongside the channel purge.
        log_counts = purge_old_logs(db, dry_run=dry_run)
        log.info(
            "log retention: error_logs=%d sessions_deleted=%d sessions_scrubbed=%d",
            log_counts["error_logs"],
            log_counts["sessions_deleted"],
            log_counts["sessions_scrubbed"],
        )

        log.info("purge complete: purged=%d dry_skipped=%d", purged, skipped)
        return 0
    except Exception:
        log.exception("purge run failed")
        db.rollback()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
