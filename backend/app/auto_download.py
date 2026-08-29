"""Automatic download of new uploads.

Makes the "Automatically sync" toggle real. Downloads are driven by SyncJob
rows that the worker claims; until now those were only ever created by
user-triggered endpoints, so a new upload was *discovered* but never
*downloaded* unless someone clicked Sync.

Two entry points, both funnelling through :func:`enqueue_downloads`:

  1. **Push** - the PubSubHubbub upload notification calls
     :func:`auto_enqueue_for_channel` the moment YouTube tells us about a new
     video, so it queues within seconds.
  2. **Sweep** - ``scripts/auto_download_sweep.py`` runs periodically and
     catches anything push missed (hub lease lapsed, we were down, the
     notification never arrived).

Deliberately scoped to NEW uploads (published after the user added the
channel). The pre-existing back catalogue is left to the explicit Sync
action, so adding a channel never silently starts a huge, surprising
download + storage bill.

Gating, in order: channel not removed -> ``active`` -> ``downloadNewVideos``
-> user's plan is active (downloads cost money). Dedup skips videos that are
already archived or already have a pending/running job.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app import access
from app.models import (
    Channel,
    SyncJob,
    User,
    UserChannel,
    UserChannelVideo,
    Video,
)

log = logging.getLogger("aether.auto_download")


def _settings_of(user_channel: UserChannel) -> Dict[str, Any]:
    try:
        return (json.loads(user_channel.data_json) or {}).get("settings") or {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _data_of(user_channel: UserChannel) -> Dict[str, Any]:
    try:
        return json.loads(user_channel.data_json) or {}
    except (json.JSONDecodeError, TypeError):
        return {}


def auto_download_enabled(db: Session, user_channel: UserChannel) -> bool:
    """Whether new uploads on this channel should auto-download for this user."""
    if user_channel.removed_at is not None:
        return False
    settings = _settings_of(user_channel)
    if not settings.get("active", True):
        return False
    # Absent => on: the UI default is on and it's the product's core promise.
    if not settings.get("downloadNewVideos", True):
        return False
    user = db.get(User, user_channel.user_id)
    if user is None or getattr(user, "payment_status", None) != "active":
        return False
    return True


# How many video jobs a user may have outstanding (pending + running) at
# once, counted across every channel they track.
#
# Not a database limit - the claim query is cheap at any depth. It bounds
# the things that are not about SQL: how many rows a channel-removal or a
# lapsed card has to cancel, how big the table gets across N users, and
# how far a wrong discovery runs before anybody notices. A 20,000-video
# channel is now one add away, so "however many the catalogue happens to
# have" is not an answer.
#
# The queue is topped up every half hour by the sweep and on every worker
# start by discovery, and the worker drains roughly 13 videos an hour, so
# 1,000 is about 75 hours of buffer. Starvation is not reachable; the cap
# only ever delays the tail of a very large catalogue.
VIDEO_JOBS_MAX_OUTSTANDING = 1000

# How fast we come back to a video that keeps failing.
#
# This used to be a hard stop at five attempts, and the stop was the bug.
# Five is the right number of FAST attempts, but it was also the last
# word: after five the queue never looked at the video again. Two of the
# owner's own three outstanding failures were caused by exactly that.
# One was a video we first tried five seconds after it was published,
# while YouTube still reported it private - all five attempts landed
# inside its first two hours, and it has been public and downloadable
# ever since. The other was a scheduled livestream we tried to grab
# before it aired.
#
# The cap could not tell "this will never work" from "this is not ready
# yet", and those are the cases where waiting is the entire fix. So the
# attempts now only decide the DELAY, and there is no last word:
#
#   first 5 attempts   as fast as the loop runs - catches a bad minute
#   next ~2 weeks      once a day - catches a video that becomes
#                      available later: published, aired, unprivated
#   after that         once a week, forever
#
# Weekly rather than daily forever because the retry runs yt-dlp on the
# user's own machine. Someone with 500 region-locked videos should not
# pay 500 pointless invocations a day for the rest of time, and nobody
# needs a 24-hour turnaround on a video that has been gone for a month.
# Manual retry still bypasses all of it.
RETRY_BURST_ATTEMPTS = 5
RETRY_DAILY_UNTIL_ATTEMPTS = 19
RETRY_DAILY = timedelta(days=1)
RETRY_WEEKLY = timedelta(days=7)


def retry_delay_for(attempts: int) -> timedelta:
    """How long to wait after ``attempts`` counting failures."""
    if attempts < RETRY_BURST_ATTEMPTS:
        return timedelta(0)
    if attempts < RETRY_DAILY_UNTIL_ATTEMPTS:
        return RETRY_DAILY
    return RETRY_WEEKLY

# Failures that say nothing about the video.
#
# The attempt counter is meant to answer "is this file downloadable?", but
# it was counting every failure equally, including ones that were purely
# our problem. The owner's own channel proved how badly that goes: the
# storage bucket filled with dead object versions and started 403ing every
# upload, yt-dlp's extractor broke for a stretch, and between them eight
# videos burned through all five attempts and were written off for good -
# every one of them perfectly downloadable, none of them ever tried again.
#
# So infrastructure trouble and upstream breakage do not count. What does
# count is a property of the video given the credentials we hold: age
# gates, members-only, private, removed. Those are real answers to the
# question, and each of them should push the next attempt further out -
# which is the other half of this, since a permission failure retried
# every 30 minutes forever is just as wrong as one abandoned entirely.
_ENVIRONMENTAL_ERROR_MARKERS = (
    "storage cap exceeded",
    "r2 put http 403",
    "r2 put http 5",
    "unable to extract yt initial data",
    "http error 5",
    "unable to download video data",
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "connection error",
    "temporarily unavailable",
    "service unavailable",
    "server returned 5",
)


def failure_counts_against_video(error: Optional[str]) -> bool:
    """Whether this failure is evidence the VIDEO cannot be downloaded.

    False for anything that describes the weather rather than the file:
    our storage rejecting the upload, yt-dlp breaking against a YouTube
    change, a timeout. Retrying those is exactly right, and counting them
    permanently disqualifies videos for being unlucky.

    Cancellations never count either - work the user called off is not a
    verdict on the video.
    """
    if not error:
        return False
    e = error.lower()
    if e.startswith("cancelled:"):
        return False
    return not any(m in e for m in _ENVIRONMENTAL_ERROR_MARKERS)


# Failures that authenticating the channel could plausibly fix. These DO
# count against a video while they stand - we should not retry a private
# video every 30 minutes forever - but the moment the worker proves it
# owns the channel, they stop being evidence of anything.
_PERMISSION_ERROR_MARKERS = (
    "sign in to confirm your age",
    "sign in to confirm",
    "private video",
    "this video is private",
    "members-only",
    "members only",
    "join this channel",
    "sign in",
)


def forgive_permission_failures(
    db: Session, *, user_id: str, channel_youtube_id: str
) -> int:
    """Clear the give-up history that authenticating has just invalidated.

    Returns how many rows were forgiven.

    Without this, authenticating a channel does nothing for the very
    videos it exists to unlock. The owner's channel had two age-gated
    videos sitting at exactly RETRY_BURST_ATTEMPTS: five honest refusals,
    correctly counted, from before there were any credentials to refuse.
    He then authenticated - the one action that fixes them - and they
    stayed written off, because the counter had no idea the world had
    changed underneath it.

    Implemented by prefixing the stored error rather than deleting rows:
    the "cancelled:" prefix is already the marker for work that must not
    count, the original text survives for anyone reading the history,
    and there is no new column.
    """
    forgiven = 0
    for job in (
        db.query(SyncJob)
        .filter(
            SyncJob.user_id == user_id,
            SyncJob.channel_id == channel_youtube_id,
            SyncJob.kind == "video",
            SyncJob.status == "failed",
            SyncJob.error.isnot(None),
        )
        .all()
    ):
        err = job.error or ""
        low = err.lower()
        if low.startswith("cancelled:"):
            continue
        if any(m in low for m in _PERMISSION_ERROR_MARKERS):
            job.error = f"cancelled: superseded by authentication | {err}"[:4000]
            forgiven += 1
    if forgiven:
        log.info(
            "forgave %d permission failures for %s on %s after authentication",
            forgiven,
            user_id,
            channel_youtube_id,
        )
    return forgiven


def enqueue_downloads(
    db: Session,
    *,
    user_id: str,
    channel_youtube_id: str,
    video_ids: Iterable[str],
) -> int:
    """Create pending SyncJobs for these videos, skipping any that are already
    archived or already have a pending/running job. Returns how many were
    created. Does NOT commit - the caller owns the transaction.

    Assumes the caller already checked the gating (see auto_download_enabled).
    """
    ids: List[str] = [v for v in dict.fromkeys(video_ids) if v]
    if not ids:
        return 0

    # Room left under the per-user cap, counted across ALL channels so six
    # channels cannot queue six caps' worth between them.
    outstanding = (
        db.query(SyncJob)
        .filter(
            SyncJob.user_id == user_id,
            SyncJob.kind == "video",
            SyncJob.status.in_(["pending", "running"]),
        )
        .count()
    )
    room = VIDEO_JOBS_MAX_OUTSTANDING - outstanding
    if room <= 0:
        log.info(
            "user %s already has %d video jobs outstanding; queueing none",
            user_id,
            outstanding,
        )
        return 0

    # kind == "video" matters. Without it a pending CAPTIONS job counted
    # as the video being in flight, so backfilling captions onto a video
    # silently blocked the video itself from ever being queued.
    in_flight: Set[str] = {
        v
        for (v,) in db.query(SyncJob.video_id).filter(
            SyncJob.user_id == user_id,
            SyncJob.channel_id == channel_youtube_id,
            SyncJob.video_id.in_(ids),
            SyncJob.kind == "video",
            SyncJob.status.in_(["pending", "running"]),
        )
    }

    archived: Set[str] = set()
    for row in db.query(UserChannelVideo).filter(
        UserChannelVideo.user_id == user_id,
        UserChannelVideo.channel_id == channel_youtube_id,
        UserChannelVideo.video_id.in_(ids),
    ):
        try:
            if (json.loads(row.data_json) or {}).get("status") == "archived":
                archived.add(row.video_id)
        except (json.JSONDecodeError, TypeError):
            continue

    # Videos that are not due for another attempt yet. Counted off the
    # job rows themselves - no new column, and durable because nothing
    # deletes terminal rows. Cancellations are excluded: work the user
    # called off must never count against the video.
    # Counted in Python rather than SQL because the decision is about
    # what the error SAYS, not just how many there are - see
    # failure_counts_against_video. The row count is small (terminal jobs
    # for one channel) so this stays a single query either way.
    #
    # We also track WHEN the last counting failure was, because attempts
    # no longer decide whether to try again, only how long to wait.
    attempts: Dict[str, int] = {}
    last_failed_at: Dict[str, datetime] = {}
    for vid, err, finished, created in db.query(
        SyncJob.video_id, SyncJob.error, SyncJob.finished_at, SyncJob.created_at
    ).filter(
        SyncJob.user_id == user_id,
        SyncJob.channel_id == channel_youtube_id,
        SyncJob.kind == "video",
        SyncJob.status == "failed",
        SyncJob.error.isnot(None),
    ):
        if not failure_counts_against_video(err):
            continue
        attempts[vid] = attempts.get(vid, 0) + 1
        when = finished or created
        if when is not None:
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            prev = last_failed_at.get(vid)
            if prev is None or when > prev:
                last_failed_at[vid] = when

    now = datetime.now(timezone.utc)
    not_due_yet: Set[str] = set()
    for vid, n in attempts.items():
        delay = retry_delay_for(n)
        if not delay:
            continue
        when = last_failed_at.get(vid)
        if when is None or (now - when) < delay:
            not_due_yet.add(vid)

    queue: List[str] = []
    for vid in ids:
        if vid in in_flight or vid in archived or vid in not_due_yet:
            continue
        if len(queue) >= room:
            log.info(
                "user %s hit the %d outstanding-job cap; %d left for the "
                "next pass",
                user_id,
                VIDEO_JOBS_MAX_OUTSTANDING,
                len(ids) - len(queue),
            )
            break
        queue.append(vid)

    if not queue:
        return 0

    # INSERT OR IGNORE against uniq_sync_jobs_active.
    #
    # The checks above catch the ordinary case; this catches the race. The
    # sweep runs in a different process from the API, so its snapshot of
    # what is in flight can go stale between the read and the commit - and
    # the loser of that race must write nothing rather than create a second
    # job that downloads the same video twice and bills the storage twice.
    # Silently dropping the duplicate is the correct outcome, so OR IGNORE
    # rather than an exception handler around a bulk flush.
    now = datetime.now(timezone.utc)
    db.execute(
        sqlite_insert(SyncJob).prefix_with("OR IGNORE"),
        [
            {
                "id": str(uuid.uuid4()),
                "user_id": user_id,
                "channel_id": channel_youtube_id,
                "video_id": vid,
                "kind": "video",
                "status": "pending",
                "progress": 0.0,
                "created_at": now,
            }
            for vid in queue
        ],
    )
    return len(queue)


def auto_enqueue_for_channel(
    db: Session,
    *,
    channel_youtube_id: str,
    video_ids: Iterable[str],
) -> int:
    """Queue the given new uploads for every subscriber of this channel who
    has auto-download on. Used by the PubSub upload notification. Returns the
    total number of jobs created across users. Caller commits.
    """
    ids = [v for v in dict.fromkeys(video_ids) if v]
    if not ids:
        return 0
    total = 0
    subscribers = (
        db.query(UserChannel)
        .filter(
            UserChannel.channel_id == channel_youtube_id,
            UserChannel.removed_at.is_(None),
        )
        .all()
    )
    for uc in subscribers:
        if not auto_download_enabled(db, uc):
            continue
        n = enqueue_downloads(
            db,
            user_id=uc.user_id,
            channel_youtube_id=channel_youtube_id,
            video_ids=ids,
        )
        if n:
            log.info(
                "auto-download: queued %d new upload(s) for user %s channel %s",
                n, uc.user_id, channel_youtube_id,
            )
        total += n
    return total


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def pending_new_uploads(
    db: Session, user_channel: UserChannel
) -> List[str]:
    """Video ids on this channel that should be backed up and are not yet.

    THE WHOLE CATALOGUE, not just new uploads. This used to skip anything
    published before the channel was added - "back catalogue, explicit
    Sync only" - which meant adding a channel to a backup service backed
    up none of the videos already on it. The user had to find and press
    Sync to get their own existing content, and nothing said so. For a
    product whose promise is "your channel is backed up", that was the
    single biggest gap in it.

    Reads both models so nothing slips through the migration gap: legacy
    UserChannelVideo rows (written by discovery) and shared-pool Video rows
    (written by the PubSub notification).
    """
    uid = user_channel.user_id
    cid = user_channel.channel_id
    out: List[str] = []

    # Videos THIS user already holds. The shared-pool loop below needs
    # this: it used to filter on Video.r2_key, which names whichever
    # single subscriber archived the file rather than the caller, so it
    # both stranded other subscribers AND happened to dedupe for the
    # first one. Removing it fixed the stranding and lost the dedupe, so
    # the per-user answer has to be computed explicitly.
    already_mine: Set[str] = set()

    # Legacy discovery rows: anything not archived yet.
    for row in db.query(UserChannelVideo).filter(
        UserChannelVideo.user_id == uid,
        UserChannelVideo.channel_id == cid,
    ):
        try:
            d = json.loads(row.data_json) or {}
        except (json.JSONDecodeError, TypeError):
            continue
        if d.get("status") == "archived":
            already_mine.add(row.video_id)
            continue
        out.append(row.video_id)

    # Shared-pool rows (e.g. PubSub-discovered) with no archived file yet.
    channel = (
        db.query(Channel).filter(Channel.youtube_id == cid).one_or_none()
    )
    if channel is not None:
        # Same access rule as the listing endpoint. Without it this
        # sweep would enqueue the CHANNEL OWNER's private and unlisted
        # videos into a mere subscriber's queue: their worker would be
        # handed the url and a presigned upload, unlisted needs no
        # cookies so the bytes would really download, and the storage
        # ledger would bill them for a file they never asked for. The
        # pool is shared; entitlement is not.
        # NOT filtered on Video.r2_key. That column belongs to the SHARED
        # pool row and names whichever single subscriber archived the file
        # - so filtering on it meant that once ANY user archived a video,
        # it stopped being queued for every OTHER subscriber, forever.
        # Whether *this* user holds it is the legacy-row question above.
        for v in db.query(Video).filter(
            Video.channel_id == channel.id,
            access.visible_video_filter(db, uid, channel.id),
        ):
            if v.youtube_id in already_mine:
                continue
            out.append(v.youtube_id)

    return list(dict.fromkeys(out))
