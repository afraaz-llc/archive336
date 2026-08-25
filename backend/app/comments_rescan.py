"""Comments rescan engine.

For one video at a time:
  1. Fetch every comment + reply via the YouTube Data API.
  2. Diff against the VideoComment rows we already have for this video.
  3. New comments -> INSERT with first_seen_at = last_seen_at = now.
  4. Already-known comments -> UPDATE last_seen_at, like_count, text
     (if changed -> bump is_edited), clear deleted_at if it was set
     (reincarnation - YouTube briefly thought it was gone but it's
     actually still there).
  5. Comments in our DB but not in the API response (and not already
     soft-deleted) -> set deleted_at = now. The "recently deleted"
     channel-wide feed reads these.

The archive philosophy: never delete rows here. A comment that
disappears from YouTube stays in our DB forever, marked deleted, so
the user can browse what was lost. That's the whole reason this
feature exists.

The scheduler / cron lives elsewhere - this module just runs a single
rescan pass against one (user, channel_id, video_id) tuple.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.google_oauth import Credentials, fetch_video_comments
from app.models import UserChannelVideo, VideoComment


log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        # YouTube API returns ISO 8601 UTC with trailing Z.
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def rescan_video_comments(
    db: Session,
    *,
    row: UserChannelVideo,
    creds: Credentials,
    channel_owner_id: Optional[str] = None,
) -> Dict[str, int]:
    """Rescan comments for a single archived video.

    Returns a counters dict for the caller to log/aggregate:
        {
          "fetched":       <int>,  # total comments+replies from the API
          "inserted":      <int>,  # newly-seen comments
          "updated":       <int>,  # known comments whose data changed
          "edited":        <int>,  # subset of updated where text changed
          "soft_deleted":  <int>,  # known comments now missing from API
          "reincarnated":  <int>,  # previously-deleted now back
        }

    Caller is responsible for db.commit() and for resolving the OAuth
    credentials (via app.oauth_loader.load_user_credentials) before
    calling this.

    `channel_owner_id` is the YouTube channel ID of the video's channel
    owner. If provided, we mark comments where author_channel_id ==
    channel_owner_id as is_by_uploader (the YouTube Data API doesn't
    expose this flag directly).
    """
    api_comments = fetch_video_comments(creds, row.video_id, order="time")
    # The Data API paginates the whole thread, so the cron always holds a
    # complete, owner-authenticated snapshot: delete freely, no debounce.
    # (The worker path calls apply_comment_snapshot directly, passing its
    # own stricter allow_deletions / deletion_grace guards.)
    return apply_comment_snapshot(
        db,
        row=row,
        api_comments=api_comments,
        channel_owner_id=channel_owner_id,
        allow_deletions=True,
        deletion_grace=None,
    )


def apply_comment_snapshot(
    db: Session,
    *,
    row: UserChannelVideo,
    api_comments: List[Dict[str, Any]],
    channel_owner_id: Optional[str],
    # Fail-safe default: a caller must OPT IN to soft-deletion. This function
    # gates a destructive write (surfacing a comment in the "recently deleted"
    # feed), so a future caller that forgets the flag deletes nothing rather
    # than wiping a comment on an uncertified fetch. The OAuth cron and the
    # worker completion both pass it explicitly.
    allow_deletions: bool = False,
    deletion_grace: Optional[timedelta] = None,
) -> Dict[str, int]:
    """Store a fetched comment snapshot against our DB rows.

    This is the single store engine shared by both fetch paths (the OAuth
    Data API cron above and the desktop worker's yt-dlp adapter). Callers
    do their own fetching, then hand the already-fetched comment list here.

    Each comment dict must carry these engine keys (the worker adapter
    builds dicts with the identical names):
        id, parent_id, author, author_channel_id, text, like_count,
        is_edited, viewer_rating_like, published_at, updated_at

    Returns the same counters dict `rescan_video_comments` documents:
        {"fetched", "inserted", "updated", "edited", "soft_deleted",
         "reincarnated"}.

    `allow_deletions` gates the soft-delete loop. A fetch that isn't
    certified owner-authenticated AND complete must pass allow_deletions
    =False: it may insert/update/reincarnate but never soft-delete, because
    a truncated snapshot (bot-check interstitial, anonymous view) would
    otherwise mark still-live comments as deleted - the worst output.

    `deletion_grace` is a free second-strike debounce (no schema cost).
    When set, a comment missing from this fetch is only soft-deleted if its
    last_seen_at is already older than (now - deletion_grace) - i.e. it was
    also absent on the previous complete fetch. A first miss is left alone;
    a second consecutive complete-fetch miss crosses the threshold.
    last_seen_at is bumped on every sighting, so this needs no extra state.

    Caller is responsible for db.commit(). `channel_owner_id` marks
    comments whose author_channel_id matches it as is_by_uploader.
    """
    counters = {
        "fetched": len(api_comments),
        "inserted": 0,
        "updated": 0,
        "edited": 0,
        "soft_deleted": 0,
        "reincarnated": 0,
    }

    if not api_comments:
        # Comments disabled, video gone, or just an empty thread. We
        # don't soft-delete in this case - the API might be lying to
        # us (region block, transient empty result). Only mark
        # deletions when we have at least one comment to anchor the
        # "we actually scraped successfully" signal.
        return counters

    now = _now()

    # Load existing rows for this video into a dict for fast lookup.
    existing = {
        c.id: c
        for c in db.query(VideoComment).filter(
            VideoComment.user_id == row.user_id,
            VideoComment.channel_id == row.channel_id,
            VideoComment.video_id == row.video_id,
        )
    }

    api_ids: set = set()
    for c in api_comments:
        cid = c["id"]
        api_ids.add(cid)
        text = c["text"]
        text_hash = _hash_text(text)
        is_uploader = (
            channel_owner_id is not None
            and c["author_channel_id"] == channel_owner_id
        )
        if cid in existing:
            existed = existing[cid]
            existed.last_seen_at = now
            # like_count is mutable in the world; refresh it always so
            # the saved value tracks the most recent observation.
            existed.like_count = c["like_count"]
            if existed.text_hash != text_hash:
                # Edited - update text + bump is_edited.
                existed.text = text
                existed.text_hash = text_hash
                existed.is_edited = True
                counters["edited"] += 1
            # API may now report it as edited even if our hashes match
            # (initial sync with already-edited text).
            if c["is_edited"] and not existed.is_edited:
                existed.is_edited = True
            # If we had previously soft-deleted this comment and it's
            # back, clear the deleted_at marker. Track this as a
            # separate counter - reincarnations are rare and worth
            # noticing.
            if existed.deleted_at is not None:
                existed.deleted_at = None
                counters["reincarnated"] += 1
            existed.viewer_rating_like = bool(c["viewer_rating_like"])
            existed.is_by_uploader = bool(is_uploader)
            existed.updated_at_remote = _parse_iso(c["updated_at"])
            counters["updated"] += 1
        else:
            new_row = VideoComment(
                id=cid,
                user_id=row.user_id,
                channel_id=row.channel_id,
                video_id=row.video_id,
                parent_comment_id=c["parent_id"],
                author=c["author"],
                author_channel_id=c["author_channel_id"],
                text=text,
                text_hash=text_hash,
                like_count=c["like_count"],
                is_edited=bool(c["is_edited"]),
                is_pinned=False,
                is_by_uploader=bool(is_uploader),
                viewer_rating_like=bool(c["viewer_rating_like"]),
                published_at=_parse_iso(c["published_at"]),
                updated_at_remote=_parse_iso(c["updated_at"]),
                first_seen_at=now,
                last_seen_at=now,
                deleted_at=None,
            )
            db.add(new_row)
            counters["inserted"] += 1

    # Anything in our DB that wasn't in the API response - soft delete.
    # Skipped entirely unless the caller certified this fetch complete
    # (allow_deletions): an incomplete/anonymous snapshot must never mark a
    # live comment deleted. Already-deleted rows are left as-is (we don't
    # refresh their deleted_at).
    if allow_deletions:
        # Second-strike debounce: when a grace window is set, only delete a
        # missing comment that was ALSO missing last time. last_seen_at is
        # bumped on every sighting, so a comment seen within one cadence is
        # missing for the first time - wait one more complete fetch.
        stale_before = now - deletion_grace if deletion_grace is not None else None
        for cid, existed in existing.items():
            if cid in api_ids:
                continue
            if existed.deleted_at is not None:
                continue
            if stale_before is not None:
                # SQLite stores naive datetimes for DateTime(timezone=True)
                # columns, so last_seen_at reads back tz-naive while
                # stale_before is tz-aware. Attach UTC before comparing or
                # the ">=" raises TypeError (mirrors the scheduler's _due()).
                last_seen = existed.last_seen_at
                if last_seen is not None and last_seen.tzinfo is None:
                    last_seen = last_seen.replace(tzinfo=timezone.utc)
                if last_seen is None or last_seen >= stale_before:
                    # First miss (or unknown last-seen) - hold off, don't delete.
                    continue
            existed.deleted_at = now
            counters["soft_deleted"] += 1

    return counters
