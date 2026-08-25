"""Daily comments-rescan cron.

TWO PATHS, PICKED PER CHANNEL BY HOW IT WAS AUTHENTICATED
--------------------------------------------------------
  - OAuth: a channel with a live Google connection (channel.google_user_id
    set) reads comments straight from the YouTube Data API through
    google_oauth.fetch_video_comments and stores them via the comments
    rescan engine. This path fetches, diffs and writes inline, per video.

  - Worker: a channel authenticated through the desktop worker never
    completes web OAuth, so channel.google_user_id stays NULL - the normal
    state for the whole Basic tier. The Data API is no help there (an API
    key reads only PUBLIC videos, and private videos are the majority of
    what we archive), but the worker already holds the user's YouTube
    cookies and can pull comments the API never could. For those channels
    this cron ENQUEUES one comment job per due video; the worker fetches,
    posts the result back, and the completion route runs the store engine.
    Nothing is fetched or written here on this path - it only queues work.

    The worker path SHIPS DARK. enqueue_comment_jobs is gated behind the
    COMMENTS_JOBS_ENABLED env flag (off by default, exactly like metadata
    jobs) and hands out nothing until every installed worker is a build that
    runs the comment kind and posts the "comments" object back. With the
    flag off this cron queues nothing on the worker path and says so.

    A channel with neither OAuth nor a live worker ownership has no comment
    source at all and stays genuinely inert - reported as such on every run
    so it never looks like a healthy zero-work channel.

The frontend still gates the "Sync comments" toggle on the channel payload's
commentsSyncAvailable, which is false for worker channels, so today only a
channel that had OAuth and later revoked it can reach the worker path with
syncComments already on. Opening the toggle to worker channels is a later
phase; this cron is wired for it ahead of that, behind the flag.

For each active channel with syncComments=True, walks every archived video
whose last_comments_sync_at is older than the configured cadence: the OAuth
path rescans inline, the worker path enqueues. Comments sync is off by
default; when it's on the cadence is automatic (quarterly if none is set).

The comments rescan engine writes new comments, soft-deletes ones that have
disappeared from YouTube (only on a fetch certified owner-authenticated AND
complete - see comments_rescan), and tracks edits via text hashing.

Cadence -> minimum age before re-rescan (same as metadata cron):
    weekly    -> 7 days
    monthly   -> 28 days
    quarterly -> 90 days
    annually  -> 365 days

Idempotent within a day:
  - OAuth path: last_comments_sync_at gets bumped on every processed video
    so a second run the same day finds nothing due.
  - Worker path: last_comments_sync_at is bumped by the completion route
    when the worker's result lands, not here. A second run before that finds
    the video still due, but enqueue_comment_jobs skips one already pending
    or running, so it tops the queue up rather than double-queueing.
  - Soft-deleted comments stay soft-deleted (we don't update deleted_at
    on rows that already have it).

Quota cost (OAuth path only) is the big variable here. One commentThreads
page = 1 unit (up to 100 top-level comments). Each non-inline reply chain is
additional units. A video with 50k comments could cost 500+ units.
At 10k daily quota that's ~20 such videos per day. The cron will
process whatever fits, and unprocessed videos pick up on the next
day naturally.

Usage:
    /opt/aether/venv/bin/python -m scripts.rescan_comments
    /opt/aether/venv/bin/python -m scripts.rescan_comments --dry
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, List

from app.comments_rescan import rescan_video_comments
from app.db import SessionLocal
from app.models import UserChannel, UserChannelVideo
from app.service_access import active_service_user_ids
from app.oauth_loader import load_user_credentials
# The worker enqueue path reuses the route layer's single switch rather than
# reimplementing it: _worker_owned_channel decides ownership, enqueue_comment_jobs
# owns the COMMENTS_JOBS_ENABLED gate, the in-flight dedup and the per-user cap.
# Same pattern the metadata enqueue helpers already follow.
from app.routes.youtube import _worker_owned_channel, enqueue_comment_jobs


log = logging.getLogger("aether.rescan_comments")


_CADENCE_DAYS: Dict[str, int] = {
    "weekly": 7,
    "monthly": 28,
    "quarterly": 90,
    "annually": 365,
}

# Fallback cadence when comments sync is on but no (or a retired
# "manual"/unknown) frequency is stored. Comments are the heaviest re-pull,
# so the automatic default is the slowest option.
_DEFAULT_CADENCE = "quarterly"


def _due(row: UserChannelVideo, cadence_days: int, now: datetime) -> bool:
    if row.last_comments_sync_at is None:
        return True
    # SQLite stores naive datetimes for DateTime(timezone=True) columns.
    # Attach UTC tzinfo so the subtraction with our tz-aware `now` works.
    last = row.last_comments_sync_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last) >= timedelta(days=cadence_days)


def _is_archived(row: UserChannelVideo) -> bool:
    try:
        data = json.loads(row.data_json)
    except json.JSONDecodeError:
        return False
    return data.get("status") == "archived"


def _settings_from_channel(channel: UserChannel) -> Optional[dict]:
    try:
        data = json.loads(channel.data_json)
    except json.JSONDecodeError:
        return None
    settings = data.get("settings") or {}
    return settings if isinstance(settings, dict) else None


def _creds_for_channel(db, channel: UserChannel):
    # No google_user_id means the channel was authenticated by the desktop
    # worker rather than by web OAuth. Comments are OAuth-only (see the
    # module docstring), so this is a dead end, not a retryable gap.
    if not channel.google_user_id:
        return None
    return load_user_credentials(
        db, channel.user_id, channel.google_user_id,
    )


def _process_channel(
    db,
    channel: UserChannel,
    cadence_days: int,
    now: datetime,
    dry: bool,
) -> Dict[str, int]:
    counters = {
        "videos_total": 0,
        "videos_due": 0,
        "comments_inserted": 0,
        "comments_soft_deleted": 0,
        "comments_edited": 0,
        "videos_with_errors": 0,
        # Set when the channel has comments sync on but no OAuth token, so
        # the summary can separate "nothing to do" from "cannot do it".
        "oauth_missing": 0,
        "videos_skipped_no_oauth": 0,
        # Worker path (no web OAuth, authenticated through the desktop worker).
        # worker_live: this run enqueued comment jobs for the channel.
        # worker_gated: worker-owned with videos due, but COMMENTS_JOBS_ENABLED
        # is off, so nothing was queued - the capability shipping dark, not a
        # fault. These two are mutually exclusive per channel.
        "worker_live": 0,
        "worker_gated": 0,
        "comment_jobs_enqueued": 0,
        "comment_jobs_remaining": 0,
    }

    videos = (
        db.query(UserChannelVideo)
        .filter(
            UserChannelVideo.user_id == channel.user_id,
            UserChannelVideo.channel_id == channel.channel_id,
        )
        .all()
    )
    counters["videos_total"] = len(videos)

    due_rows = [
        v for v in videos
        if _is_archived(v) and _due(v, cadence_days, now)
    ]
    counters["videos_due"] = len(due_rows)

    # Route by how the channel was authenticated. No google_user_id means it
    # was authenticated through the desktop worker (the Basic-tier norm) rather
    # than web OAuth. That path is handled - and reported - separately, ahead of
    # the nothing-due shortcut so an unfetchable channel still says so every run.
    if not channel.google_user_id:
        return _process_no_oauth(db, channel, due_rows, dry, counters)

    # ---- OAuth path (unchanged): read comments straight from the Data API. ----
    if not due_rows:
        return counters

    creds = _creds_for_channel(db, channel)
    if creds is None:
        # Distinct from the case above: there IS a stored connection, it is
        # just unusable (marked disconnected, undecryptable, or the refresh
        # failed). That one is fixable by reconnecting.
        counters["oauth_missing"] = 1
        counters["videos_skipped_no_oauth"] = len(due_rows)
        log.warning(
            "channel %s/%s: %d videos due but the stored Google connection "
            "is unusable (disconnected, or the token refresh failed), "
            "skipping",
            channel.user_id, channel.channel_id, len(due_rows),
        )
        return counters

    if dry:
        log.info(
            "[dry] channel %s/%s: would rescan comments for %d videos",
            channel.user_id, channel.channel_id, len(due_rows),
        )
        return counters

    # Comments are rescanned per-video (no batch path in the API). One
    # video can take many quota units depending on comment volume.
    # Commit after each so a mid-loop failure doesn't lose progress.
    for v in due_rows:
        try:
            stats = rescan_video_comments(
                db,
                row=v,
                creds=creds,
                channel_owner_id=channel.channel_id,
            )
            v.last_comments_sync_at = now
            db.commit()
        except Exception as e:
            db.rollback()
            counters["videos_with_errors"] += 1
            log.exception(
                "channel %s/%s video %s comment-rescan failed: %s",
                channel.user_id, channel.channel_id, v.video_id, e,
            )
            continue

        counters["comments_inserted"] += stats["inserted"]
        counters["comments_soft_deleted"] += stats["soft_deleted"]
        counters["comments_edited"] += stats["edited"]
        if (
            stats["inserted"]
            or stats["soft_deleted"]
            or stats["edited"]
            or stats["reincarnated"]
        ):
            log.info(
                "  video %s: +%d -%d edited=%d reincarnated=%d",
                v.video_id,
                stats["inserted"], stats["soft_deleted"],
                stats["edited"], stats["reincarnated"],
            )

    return counters


def _process_no_oauth(
    db,
    channel: UserChannel,
    due_rows: List[UserChannelVideo],
    dry: bool,
    counters: Dict[str, int],
) -> Dict[str, int]:
    """Handle a channel with no web OAuth - the normal Basic-tier state.

    Those users authenticate their channel through the desktop worker and never
    complete web OAuth, so channel.google_user_id stays NULL. The worker holds
    their YouTube cookies and can read comments the Data API cannot, so a
    worker-owned channel gets its due videos handed to that worker. A channel
    with neither OAuth nor a live worker ownership has no source and stays inert.

    This only ENQUEUES. The worker fetches and posts the result back, and the
    completion route runs the store engine and bumps last_comments_sync_at, so
    nothing here writes comments or moves that timestamp. enqueue_comment_jobs
    owns the capability switch (COMMENTS_JOBS_ENABLED), the ownership recheck and
    the in-flight dedup; it creates nothing while the switch is off, so calling
    it on the dark path is a true no-op.
    """
    uid, cid = channel.user_id, channel.channel_id

    # Read ownership from the column state only - no loader/token call, which
    # would hit the network and could fire a disconnect email.
    if _worker_owned_channel(db, uid, cid) is None:
        # No OAuth and no worker to act as the account: nothing in this codebase
        # can read these comments. Reported ahead of the due-count check so an
        # unfetchable channel says so on every run rather than looking idle.
        counters["oauth_missing"] = 1
        counters["videos_skipped_no_oauth"] = len(due_rows)
        log.warning(
            "channel %s/%s: comments sync is ON but this channel has neither a "
            "Google OAuth token nor a worker-authenticated owner, and comments "
            "have no other source. Nothing was fetched and nothing can be (%d "
            "of %d archived videos due). This channel is inert, not idle.",
            uid, cid, len(due_rows), counters["videos_total"],
        )
        return counters

    # Worker-owned from here down.
    if not due_rows:
        # Healthy, just nothing aged past its cadence today - quiet, exactly
        # like the OAuth path's nothing-due shortcut.
        return counters

    if dry:
        log.info(
            "[dry] channel %s/%s: worker path, would enqueue comment jobs for "
            "up to %d due video(s) (subject to COMMENTS_JOBS_ENABLED and the "
            "outstanding-jobs cap)",
            uid, cid, len(due_rows),
        )
        return counters

    outcome = enqueue_comment_jobs(
        db, user_id=uid, channel_id=cid,
        video_ids=[v.video_id for v in due_rows],
    )

    if not outcome["enabled"]:
        # Capability ships dark behind COMMENTS_JOBS_ENABLED. Until it flips this
        # channel queues nothing - and enqueue_comment_jobs created nothing, so
        # this really is a no-op. Logged plainly rather than reusing the "no
        # source" line above, which would now be a lie: the source (the worker)
        # exists and is merely gated.
        counters["worker_gated"] = 1
        log.info(
            "channel %s/%s: worker-owned with %d due video(s), but comment jobs "
            "are gated off (COMMENTS_JOBS_ENABLED); queued nothing. Dark by "
            "design, not a fault.",
            uid, cid, len(due_rows),
        )
        return counters

    counters["worker_live"] = 1
    counters["comment_jobs_enqueued"] = outcome["enqueued"]
    counters["comment_jobs_remaining"] = outcome["remaining"]
    log.info(
        "channel %s/%s: worker path - enqueued %d comment job(s) across %d due "
        "video(s) (%d already in flight, %d deferred over the outstanding cap)",
        uid, cid, outcome["enqueued"], len(due_rows),
        outcome["skipped_in_flight"], outcome["remaining"],
    )
    return counters


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Daily comments rescan")
    parser.add_argument(
        "--dry", action="store_true",
        help="Skip API + DB writes; just log what would run.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )

    now = datetime.now(timezone.utc)
    grand = {
        # Every live channel we looked at, whether or not comments sync is
        # on. Without this the old summary's "channels=0" was ambiguous
        # between "no channels" and "no channel wants comments".
        "channels_seen": 0,
        "channels_comments_on": 0,
        # Comments sync on but the channel itself is paused. Counted so the
        # closing warning does not tell an operator "nobody enabled it" when
        # somebody did and the pause is what stopped it.
        "channels_paused": 0,
        "channels_inert_no_oauth": 0,
        # Worker path totals. channels_worker_live: had comment jobs enqueued
        # this run. channels_worker_gated: worker-owned with work due but the
        # capability is still dark (COMMENTS_JOBS_ENABLED off).
        "channels_worker_live": 0,
        "channels_worker_gated": 0,
        "comment_jobs_enqueued": 0,
        "comment_jobs_remaining": 0,
        "videos_due": 0,
        "videos_skipped_no_oauth": 0,
        "inserted": 0,
        "soft_deleted": 0,
        "edited": 0,
        "errors": 0,
    }

    db = SessionLocal()
    try:
        # Paused accounts spend nothing. A failed card pauses backups (the
        # owner's call), so this uses the same predicate as the HTTP gate -
        # see app/service_access.py. Existing archives are untouched; only
        # NEW work stops, so restoring service is just the column flipping
        # back.
        entitled = active_service_user_ids(db)
        channels = (
            db.query(UserChannel)
            .filter(
                UserChannel.removed_at.is_(None),
                UserChannel.user_id.in_(entitled),
            )
            .all()
        )
        for ch in channels:
            grand["channels_seen"] += 1
            settings = _settings_from_channel(ch)
            if not settings or not settings.get("active", True):
                if settings and settings.get("syncComments", False):
                    grand["channels_paused"] += 1
                continue
            if not settings.get("syncComments", False):
                continue
            # Gated on syncComments above, so a channel only reaches here with
            # comments sync on. Automatic-only: absent/retired/unknown cadence
            # falls back to the quarterly default rather than skipping.
            freq = settings.get("commentsRefreshFrequency") or _DEFAULT_CADENCE
            cadence_days = _CADENCE_DAYS.get(freq, _CADENCE_DAYS[_DEFAULT_CADENCE])

            c = _process_channel(db, ch, cadence_days, now, args.dry)
            grand["channels_comments_on"] += 1
            grand["channels_inert_no_oauth"] += c["oauth_missing"]
            grand["channels_worker_live"] += c["worker_live"]
            grand["channels_worker_gated"] += c["worker_gated"]
            grand["comment_jobs_enqueued"] += c["comment_jobs_enqueued"]
            grand["comment_jobs_remaining"] += c["comment_jobs_remaining"]
            grand["videos_due"] += c["videos_due"]
            grand["videos_skipped_no_oauth"] += c["videos_skipped_no_oauth"]
            grand["inserted"] += c["comments_inserted"]
            grand["soft_deleted"] += c["comments_soft_deleted"]
            grand["edited"] += c["comments_edited"]
            grand["errors"] += c["videos_with_errors"]
    finally:
        db.close()

    log.info(
        "comments rescan summary: channels_seen=%d comments_sync_on=%d "
        "paused=%d inert_no_oauth=%d worker_live=%d worker_gated=%d "
        "videos_due=%d videos_skipped_no_oauth=%d comment_jobs_enqueued=%d "
        "comment_jobs_deferred=%d inserted=%d soft_deleted=%d edited=%d "
        "errors=%d",
        grand["channels_seen"], grand["channels_comments_on"],
        grand["channels_paused"], grand["channels_inert_no_oauth"],
        grand["channels_worker_live"], grand["channels_worker_gated"],
        grand["videos_due"], grand["videos_skipped_no_oauth"],
        grand["comment_jobs_enqueued"], grand["comment_jobs_remaining"],
        grand["inserted"], grand["soft_deleted"], grand["edited"],
        grand["errors"],
    )

    # A run that queued or fetched nothing can look identical to a healthy idle
    # run, so spell out which reason applied. The OAuth path fetches inline; the
    # worker path only enqueues (and does nothing at all while the capability is
    # dark), so "did nothing" here is often by-design, not a fault.
    if grand["channels_comments_on"] == 0 and grand["channels_paused"]:
        log.warning(
            "%d channel(s) have comments sync on but are paused, so this job "
            "did nothing this run.",
            grand["channels_paused"],
        )
    elif grand["channels_comments_on"] == 0:
        log.warning(
            "no channel has comments sync enabled, so this job did nothing. "
            "The setting is offered to OAuth-connected channels today; worker "
            "channels reach it only once commentsSyncAvailable is opened to "
            "them. See the module docstring."
        )
    else:
        # Not mutually exclusive: a run can have both OAuth-inert channels and
        # worker channels sitting behind the dark flag, and each deserves its
        # own honest line rather than a single catch-all.
        if grand["channels_inert_no_oauth"]:
            log.warning(
                "%d channel(s) with comments sync on had no source at all - no "
                "Google OAuth and no worker owner - so nothing was fetched for "
                "them. See the per-channel warnings above for which case each "
                "one hit.",
                grand["channels_inert_no_oauth"],
            )
        if grand["channels_worker_gated"]:
            log.warning(
                "%d worker-owned channel(s) had videos due but comment jobs are "
                "gated off (COMMENTS_JOBS_ENABLED), so nothing was queued for "
                "them. This is the capability shipping dark, not a fault; it "
                "goes live once every worker is a build that runs the comment "
                "kind.",
                grand["channels_worker_gated"],
            )

    return 0 if grand["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
