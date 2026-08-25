"""Daily metadata-rescan cron.

Runs once a day on a systemd timer. Metadata sync is automatic for every
active channel — for each user/channel pair it finds archived videos whose
last_metadata_sync_at is older than the configured cadence and runs them
through the rescan engine in batches of 50 (one YouTube API quota unit per
batch). Channels with no explicit cadence fall back to monthly.

The script is idempotent within a day:
  - last_metadata_sync_at gets bumped on every processed row, so a
    second run the same day finds nothing due.
  - The HEAD/SHA dedup in the rescan engine means thumbnails aren't
    re-fetched unless they've actually changed.

Two per-video paths, picked per channel by whether web OAuth resolves:
  - OAuth: the full videos.list rescan, every versioned field.
  - No OAuth (the normal case - Basic-tier users authenticate through the
    desktop worker and never do web OAuth): channel-tab enumeration, which
    can only answer "is this public video still listed". No metadata is
    refreshed on that path and only public videos are evaluated. See
    _process_via_enumeration.

Cadence -> minimum age before re-rescan:
    weekly    -> 7 days
    monthly   -> 28 days  (one calendar month-ish)
    quarterly -> 90 days
    annually  -> 365 days

We intentionally use "minimum age" rather than "exactly N days" so a
late cron run still does the work it skipped on the missed day. Worst
case: a video gets rescanned a day late.

Usage:
    /opt/aether/venv/bin/python -m scripts.rescan_metadata
    /opt/aether/venv/bin/python -m scripts.rescan_metadata --dry
        # walks the same loop but doesn't actually call the API or
        # mutate the DB - useful for verifying which channels/videos
        # would be processed.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from app import channel_rescan
from app.db import SessionLocal
from app.metadata_rescan import (
    enumeration_can_see_row,
    notify_confirmed_removals,
    reconcile_against_enumeration,
    rescan_videos_batch,
)
from app.models import UserChannel, UserChannelVideo
from app.service_access import active_service_user_ids
from app.oauth_loader import load_user_credentials
from app.youtube_scrape import fetch_channel_videos


log = logging.getLogger("aether.rescan_metadata")


# Cadence -> minimum days between successive rescans for a single video.
_CADENCE_DAYS: Dict[str, int] = {
    "weekly": 7,
    "monthly": 28,
    "quarterly": 90,
    "annually": 365,
}

# How deep the no-OAuth path enumerates a channel's /videos tab, and the
# entry count that implies. fetch_channel_videos caps at 30 entries per page,
# so this is the most videos one enumeration can ever report. We pass the page
# count explicitly rather than lean on the default because the absence logic
# needs to know where the ceiling is: a listing that reaches it might be
# truncated, and a video we never saw is not a video that is gone.
_ENUM_MAX_PAGES = 30
_ENUM_ENTRY_CEILING = _ENUM_MAX_PAGES * 30

# Fallback when a channel has no (or a retired "manual"/unknown) cadence.
# The service is automatic-only now, so every active channel gets rescanned
# rather than being skipped.
_DEFAULT_CADENCE = "monthly"


def _due(row: UserChannelVideo, cadence_days: int, now: datetime) -> bool:
    """A video is due for rescan if it's never been rescanned, or its
    last_metadata_sync_at is older than the cadence threshold."""
    if row.last_metadata_sync_at is None:
        return True
    # SQLite returns naive datetimes for DateTime(timezone=True). Pin
    # to UTC so subtraction with the tz-aware `now` works.
    last = row.last_metadata_sync_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last) >= timedelta(days=cadence_days)


def _is_archived(row: UserChannelVideo) -> bool:
    """Only videos that have actually been downloaded are eligible -
    discovered-but-not-archived rows have no committed metadata to refresh."""
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
    """Resolve live OAuth credentials for this channel. Returns None
    if no connection exists, the connection is disconnected, or the
    refresh attempt fails (load_user_credentials handles that flow
    including persisting a freshly-refreshed token)."""
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
    settings: Optional[dict] = None,
) -> Dict[str, int]:
    """Process one channel. Returns counters for the summary log line.

    ``settings`` is the channel's settings blob; it carries the per-field
    history flags the rescan engine honors."""
    counters = {
        "videos_total": 0,
        "videos_due": 0,
        "videos_changed": 0,
        "batches": 0,
        "errors": 0,
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

    due_rows = [v for v in videos if _is_archived(v) and _due(v, cadence_days, now)]
    counters["videos_due"] = len(due_rows)
    if not due_rows:
        return counters

    creds = _creds_for_channel(db, channel)
    if creds is None:
        # No web OAuth. That's the normal state for the whole Basic tier -
        # those users authenticate their channel through the desktop worker
        # and never touch the Data API - so this is the path that actually
        # runs in production, not an error case.
        return _process_via_enumeration(db, channel, due_rows, now, dry, counters)

    if dry:
        log.info(
            "[dry] channel %s/%s: OAuth path, would rescan %d videos in %d batches",
            channel.user_id, channel.channel_id,
            len(due_rows), (len(due_rows) + 49) // 50,
        )
        return counters

    log.info(
        "channel %s/%s: OAuth path, rescanning %d due videos in %d batches",
        channel.user_id, channel.channel_id,
        len(due_rows), (len(due_rows) + 49) // 50,
    )

    # Removals confirmed anywhere in this channel's batches, collected here
    # and mailed once at the end. The batch engine would otherwise send one
    # email per 50 rows, so a channel with 120 due videos could produce three
    # separate emails for a single sweep.
    removals: Dict[Tuple[str, str], List[str]] = {}

    # Walk the due list in batches of 50. fetch_video_details accepts up
    # to 50 IDs per API call (1 quota unit each). We commit after every
    # batch so a mid-loop failure doesn't lose earlier progress.
    for i in range(0, len(due_rows), 50):
        batch = due_rows[i : i + 50]
        try:
            changes = rescan_videos_batch(
                db, rows=batch, creds=creds, settings=settings,
                removal_sink=removals,
            )
            db.commit()
        except Exception as e:
            db.rollback()
            counters["errors"] += 1
            log.exception(
                "channel %s/%s batch %d failed: %s",
                channel.user_id, channel.channel_id, i // 50, e,
            )
            # One failed batch shouldn't kill the whole channel - move on
            # to the next batch. The retry comes naturally on tomorrow's
            # cron run (last_metadata_sync_at didn't get bumped because
            # we rolled back).
            continue

        counters["batches"] += 1
        counters["videos_changed"] += len(changes)
        for vid, ch in changes.items():
            log.info(
                "  video %s changed fields: %s",
                vid, sorted(ch.keys()),
            )

    log.info(
        "channel %s/%s: OAuth path done - %d batches ok, %d errors, "
        "%d videos changed, %d removals confirmed",
        channel.user_id, channel.channel_id,
        counters["batches"], counters["errors"], counters["videos_changed"],
        sum(removals.values()),
    )

    # One mail per channel per sweep, after every batch has had its say.
    # Best-effort by contract: a mail failure must not fail the rescan or
    # undo the marks, which are already committed above.
    if removals:
        notify_confirmed_removals(db, removals)

    return counters


def _process_via_enumeration(
    db,
    channel: UserChannel,
    due_rows: List[UserChannelVideo],
    now: datetime,
    dry: bool,
    counters: Dict[str, int],
) -> Dict[str, int]:
    """Upkeep for a channel with no OAuth, driven by the public /videos tab.

    The Data API needs credentials we don't have here, and the per-video watch
    page is not a usable substitute from a datacenter IP: YouTube serves the
    bot interstitial as a LOGIN_REQUIRED playability status, which is
    indistinguishable from a genuinely private video. Channel-tab enumeration
    is the one server-side signal that answers reliably, so it's the only one
    this path uses, and it answers exactly one question: is this video still
    on the channel's public listing?

    That means the only videos this path can evaluate are the ones the listing
    would show. See metadata_rescan.enumeration_can_see_row - everything
    private, unlisted, members-only or simply unknown is left completely
    alone, because their absence from a public listing is not evidence of
    anything and an absence strike against them would end in an email telling
    the user their private videos had been deleted. Public rows are handed
    over as candidates, but the reconciler only strikes ones it has actually
    seen on the tab before (metadata_rescan._absence_is_evaluable) - the
    /videos tab carries no Shorts and no livestreams, so "public" alone would
    condemn every Short in the archive.
    """
    uid, cid = channel.user_id, channel.channel_id
    visible = [r for r in due_rows if enumeration_can_see_row(r)]
    invisible = len(due_rows) - len(visible)
    if not visible:
        # Nothing enumeration can speak to. Note that the skipped rows keep
        # their old last_metadata_sync_at (we didn't check them, so we won't
        # claim we did), which is exactly why this gate is on the visible
        # subset: otherwise a channel of private videos would look "due"
        # forever and re-enumerate every single day.
        log.info(
            "channel %s/%s: no OAuth, enumeration path has nothing to check "
            "(%d due, all non-public)",
            uid, cid, len(due_rows),
        )
        return counters

    if dry:
        log.info(
            "[dry] channel %s/%s: no OAuth, would enumerate the channel tab "
            "and check %d public videos for absence (%d non-public skipped)",
            uid, cid, len(visible), invisible,
        )
        return counters

    listing = fetch_channel_videos(cid, max_pages=_ENUM_MAX_PAGES)
    if listing is None:
        # Failed probe, not a channel that emptied itself. Mark nothing, don't
        # count it as a script error either - a YouTube blip shouldn't fail the
        # cron run. Tomorrow retries, since we bumped no timestamps.
        log.warning(
            "channel %s/%s: enumeration path - channel-tab fetch failed, "
            "marking nothing removed",
            uid, cid,
        )
        return counters

    present = [str(v.get("id") or "") for v in listing]
    present = [v for v in present if v]
    if not present:
        # Same guard as the API path's empty response: zero entries while we
        # hold public rows is a block or a rate-limit far more often than it
        # is every video disappearing at once.
        log.warning(
            "channel %s/%s: enumeration path - channel tab returned no videos "
            "while %d public rows exist, treating as a failed probe",
            uid, cid, len(visible),
        )
        return counters

    # Truncation guard. The enumeration stops at a fixed ceiling, so a channel
    # that fills it may have more public videos we simply never saw - and a
    # video we never saw must not be treated as a video that is gone. When the
    # listing comes back at the ceiling we hand the reconciler only the rows we
    # positively found: those still get their sighting (banked strikes cleared,
    # timestamp bumped) and nothing can take a strike.
    rows_to_check = visible
    truncated = len(present) >= _ENUM_ENTRY_CEILING
    if truncated:
        present_set = set(present)
        rows_to_check = [r for r in visible if r.video_id in present_set]
        log.warning(
            "channel %s/%s: enumeration path - channel tab returned %d entries, "
            "at or above the %d ceiling, so the listing may be truncated. "
            "Recording sightings only, evaluating no absences",
            uid, cid, len(present), _ENUM_ENTRY_CEILING,
        )

    removals: Dict[Tuple[str, str], List[str]] = {}
    try:
        stats = reconcile_against_enumeration(
            db,
            rows=rows_to_check,
            present_video_ids=present,
            now=now,
            removal_sink=removals,
        )
        db.commit()
    except Exception as e:
        db.rollback()
        counters["errors"] += 1
        log.exception("channel %s/%s: enumeration path failed: %s", uid, cid, e)
        return counters

    counters["videos_changed"] += stats["confirmed_removed"]
    log.info(
        "channel %s/%s: no OAuth, enumeration path done - %d listed on the "
        "channel tab, %d public videos checked (%d seen, %d absent, "
        "%d absent-but-never-tab-seen so not evaluated, %d removals "
        "confirmed), %d non-public skipped, 0 metadata fields refreshed "
        "(enumeration can't supply them)",
        uid, cid, len(present), stats["checked"], stats["seen"],
        stats["absent"], stats["unproven"], stats["confirmed_removed"],
        invisible,
    )

    if removals:
        notify_confirmed_removals(db, removals)

    return counters


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Daily metadata rescan")
    parser.add_argument(
        "--dry", action="store_true",
        help="Don't hit the API or mutate the DB - just log what would run.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )

    now = datetime.now(timezone.utc)
    grand = {"channels": 0, "videos_changed": 0, "videos_due": 0, "errors": 0}

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
            settings = _settings_from_channel(ch)
            if not settings or not settings.get("active", True):
                continue
            # Automatic-only: absent, retired ("manual"), or unrecognized
            # cadences fall back to the monthly default so the channel is
            # still rescanned rather than silently skipped.
            freq = settings.get("metadataRefreshFrequency") or _DEFAULT_CADENCE
            cadence_days = _CADENCE_DAYS.get(freq, _CADENCE_DAYS[_DEFAULT_CADENCE])

            # Channel-level info (about / avatar / stats) refreshes on the same
            # cadence, independent of the per-video rescan below. Best-effort +
            # its own commit so a hiccup here never blocks video rescanning.
            if not args.dry:
                try:
                    ch_changed = channel_rescan.refresh_channel_info(
                        db,
                        user_channel=ch,
                        settings=settings,
                        cadence_days=cadence_days,
                        now=now,
                    )
                    # The commit and the log deliberately have DIFFERENT
                    # conditions - do not re-couple them. refresh_channel_info
                    # always writes bookkeeping before it returns
                    # (lastChannelInfoSyncAt, the avatar sha, the
                    # channelInfoFailures strike counter), whether or not the
                    # user would see a change. Committing only on a visible
                    # change threw all of that away at db.close(): the
                    # termination strike counter could never reach two, the
                    # cadence gate never saw a previous run so we re-scraped
                    # daily regardless of the user's setting, and the avatar
                    # hash short-circuit never fired so we re-downloaded the
                    # avatar every single day. The log stays conditional
                    # because it genuinely is about visible changes.
                    db.commit()
                    if ch_changed:
                        log.info(
                            "channel %s/%s info refreshed: %s",
                            ch.user_id, ch.channel_id, sorted(ch_changed),
                        )
                except Exception:
                    db.rollback()
                    log.exception(
                        "channel-info refresh failed for %s/%s",
                        ch.user_id, ch.channel_id,
                    )

            counters = _process_channel(
                db, ch, cadence_days, now, args.dry, settings=settings
            )
            grand["channels"] += 1
            grand["videos_due"] += counters["videos_due"]
            grand["videos_changed"] += counters["videos_changed"]
            grand["errors"] += counters["errors"]

    finally:
        db.close()

    log.info(
        "rescan summary: channels=%d videos_due=%d videos_changed=%d errors=%d",
        grand["channels"], grand["videos_due"],
        grand["videos_changed"], grand["errors"],
    )
    return 0 if grand["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
