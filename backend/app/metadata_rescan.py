"""Versioned metadata rescan engine.

When a metadata-sync run fires for a single video, we:
  1. Pull the current YouTube state via the Data API (1 quota unit).
  2. Compare against the stored `data_json` blob field by field.
  3. For any field that changed, write a snapshot row capturing the
     OLD value with the timespan it was active, then update data_json
     with the new value.
  4. For thumbnail changes, copy the new image to a fresh, timestamped
     R2 key and snapshot the OLD R2 key into history. Old image bytes
     are never deleted - the user paid to archive them.
  5. Bump `last_metadata_sync_at` on UserChannelVideo so the open-ended
     "current value active since X" tail is bounded.

Versioned fields handled here:
    title | description | tags | thumbnail | privacy

`captionLanguages` is also versioned but populated by the caption
worker pipeline, not the metadata API, so it's not touched here.

This module also owns the shared removal detector (the two-strike debounce
plus the empty-response guard). It lives here because the scheduled rescan
is the path that runs it automatically; the manual sync endpoint in
routes/youtube.py imports the same helpers so both paths agree on what
counts as evidence that a video is gone. reconcile_against_enumeration()
below runs that same detector for channels that have no OAuth at all, using
the public channel listing as its evidence.

The actual scheduling/cron lives elsewhere - this module is just the
engine that runs against a single (user, channel_id, video_id).
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import requests
from sqlalchemy.orm import Session

from app import r2, r2_paths, storage_ledger
from app.google_oauth import Credentials, fetch_video_details
from app.models import (
    UserChannel,
    UserChannelVideo,
    VideoFieldSnapshot,
)


log = logging.getLogger(__name__)


# Fields that we version through this rescan engine. Adding a new
# field means: (1) adding it here, (2) implementing its extractor in
# _extract_from_api(), and (3) deciding equality (_values_equal).
VERSIONED_FIELDS = ("title", "description", "tags", "thumbnail", "privacy")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Removal detection (shared by the manual sync endpoint and the daily cron).
#
# Marking a video deleted_on_youtube is the one write in this codebase that
# can, if it misfires, tell a user their archive's source is gone and email
# them about it. A single absence is not evidence: the Data API drops items
# on quota errors, partial outages and transient 5xx, and the public watch
# page returns an unrecognised playabilityStatus for everything from a region
# block to a rate-limit interstitial.
#
# So absence is debounced the same way channel termination already is in
# channel_rescan._apply_termination_signal: it takes two strikes, and the
# strikes have to be far enough apart to be independent observations rather
# than one outage seen twice. Only the confirming strike flips the status and
# releases the notification.
# ---------------------------------------------------------------------------

# Consecutive misses required before a video is declared gone.
REMOVAL_STRIKES_REQUIRED = 2

# Two misses inside this window are treated as the same incident and only
# count once. Without it, hammering Sync twice in a row would "confirm" a
# removal during a 10-second YouTube blip.
REMOVAL_MIN_STRIKE_GAP_SECONDS = 15 * 60


def _parse_iso_utc(raw: Any) -> Optional[datetime]:
    """Parse an ISO timestamp out of data_json, pinned to UTC. Returns None
    for anything unparseable so callers treat it as 'no prior observation'."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def clear_removal_marks(data: Dict[str, Any]) -> None:
    """The video was seen on YouTube this run: wipe the removal bookkeeping
    so a future absence starts again from strike zero, and drop the deletion
    mark. Callers own the status field (whether a resurrected row goes back
    to 'archived' or 'discovered' depends on whether we hold the file)."""
    data["removalMissCount"] = 0
    data["lastMissAt"] = None
    data["deletedOnYoutubeAt"] = None
    # Diagnostics describe the last time the video was unavailable; keeping
    # them next to a healthy row would just mislead whoever reads them later.
    data["lastUnavailableStatus"] = None


def note_video_missing(
    data: Dict[str, Any],
    *,
    now: datetime,
    evidence: Optional[Tuple[str, str]] = None,
) -> bool:
    """Record one 'this video was not where we expected it' observation.

    Mutates ``data`` in place. Returns True ONLY on the strike that actually
    flips the row to deleted_on_youtube, so callers can use the return value
    directly as their "new transition" counter - no email on strike one, no
    second email on strike three.

    ``evidence`` is the raw (playabilityStatus.status, reason) pair when the
    caller probed the watch page. Only the status TOKEN is persisted; the
    reason string is dropped here and belongs in the caller's log if anyone
    wants it. That split is deliberate: anything written into data_json
    escapes. complete_sync_job snapshots the whole blob into
    Video.metadata_json, and archive.video_response_payload starts every
    client payload with dict(that snapshot), so a stored key reaches the
    browser whether or not a component renders it. YouTube's reason prose is
    a cause claim ("removed for violating..."), it is localised, unversioned,
    and this verdict collapses region blocks and rate-limit interstitials into
    the same bucket. We can't stand behind it, so it never gets written
    anywhere a user could read it.
    """
    now_iso = now.isoformat()
    data["lastYoutubeCheckAt"] = now_iso
    if evidence is not None:
        data["lastUnavailableStatus"] = evidence[0]

    # Already confirmed gone. Keep the counter where it is (a later sighting
    # resets it via clear_removal_marks) and never re-report the transition.
    # Backfill the timestamp for pre-existing rows that never got one.
    if data.get("status") == "deleted_on_youtube":
        if not data.get("deletedOnYoutubeAt"):
            data["deletedOnYoutubeAt"] = now_iso
        return False

    try:
        strikes = int(data.get("removalMissCount") or 0)
    except (TypeError, ValueError):
        # Garbage in the blob - restart the count rather than blow up a sync.
        strikes = 0
    last_miss = _parse_iso_utc(data.get("lastMissAt"))
    if (
        strikes
        and last_miss is not None
        and (now - last_miss).total_seconds() < REMOVAL_MIN_STRIKE_GAP_SECONDS
    ):
        # Same incident as the strike we already have. Deliberately do NOT
        # refresh lastMissAt, otherwise a run of rapid retries would keep
        # pushing the window forward and the second strike could never land.
        return False

    strikes += 1
    data["removalMissCount"] = strikes
    data["lastMissAt"] = now_iso
    if strikes < REMOVAL_STRIKES_REQUIRED:
        return False

    data["status"] = "deleted_on_youtube"
    data["deletedOnYoutubeAt"] = data.get("deletedOnYoutubeAt") or now_iso
    return True


def _pick_best_thumbnail(thumbs: Dict[str, Any]) -> Optional[str]:
    """Pick the highest-resolution thumbnail URL the API returned.

    The API returns a dict keyed by quality (default, medium, high,
    standard, maxres) - not all keys are present for every video.
    Preference order is maxres -> standard -> high -> medium -> default.
    """
    for key in ("maxres", "standard", "high", "medium", "default"):
        entry = thumbs.get(key)
        if entry and entry.get("url"):
            return entry["url"]
    return None


def _parse_view_count(raw: Any) -> Optional[int]:
    """YouTube returns statistics.viewCount as a string; coerce to int.
    None when absent or unparseable (e.g. stats hidden by the creator)."""
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _extract_from_api(item: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the versioned fields out of a YouTube videos.list item.

    Returned dict mirrors the shape we store in data_json so the diff
    is field-by-field equality.
    """
    snippet = item.get("snippet") or {}
    status = item.get("status") or {}
    statistics = item.get("statistics") or {}

    return {
        "title": snippet.get("title") or "",
        "description": snippet.get("description") or "",
        "tags": list(snippet.get("tags") or []),
        "thumbnail_url": _pick_best_thumbnail(snippet.get("thumbnails") or {}),
        # None, never a default. An absent status part means the API did not
        # tell us, and substituting "public" would demote a private video and
        # write history saying the creator published it. The caller skips the
        # field on None rather than guessing.
        "privacy": (
            status.get("privacyStatus").lower()
            if isinstance(status.get("privacyStatus"), str)
            and status.get("privacyStatus")
            else None
        ),
        "viewCount": _parse_view_count(statistics.get("viewCount")),
    }


# Per-field history flags in the channel settings blob. Fields absent from
# this map (title, privacy) are always versioned — they're not user-facing
# toggles. Absent/None settings default every flag to on.
_FIELD_HISTORY_FLAG: Dict[str, str] = {
    "description": "saveDescriptionHistory",
    "tags": "saveTagsHistory",
}


def _history_enabled(settings: Optional[Dict[str, Any]], flag: str) -> bool:
    """Whether a per-field history flag is on. Defaults to True when the
    setting is missing so behavior is unchanged for channels saved before
    the flags existed."""
    if not settings:
        return True
    return bool(settings.get(flag, True))


def _values_equal(field: str, old: Any, new: Any) -> bool:
    """Field-aware equality. Tags is a list and we treat order as
    unimportant (YouTube reorders them sometimes); everything else is
    plain ==.
    """
    if field == "tags":
        return sorted(old or []) == sorted(new or [])
    return (old or "") == (new or "")


def _captured_at_for_previous_value(
    db: Session,
    *,
    user_id: str,
    video_id: str,
    field: str,
    fallback: datetime,
) -> datetime:
    """When was the value currently in data_json first observed?

    For a field that has never changed before, that's the original
    archive time (fallback). For a field with prior history, that's
    the most recent snapshot's superseded_at - the moment the value
    we're about to retire was first set.
    """
    prior = (
        db.query(VideoFieldSnapshot)
        .filter(
            VideoFieldSnapshot.user_id == user_id,
            VideoFieldSnapshot.video_id == video_id,
            VideoFieldSnapshot.field == field,
        )
        .order_by(VideoFieldSnapshot.superseded_at.desc())
        .first()
    )
    if prior:
        return prior.superseded_at
    return fallback


def _archived_at_iso_to_dt(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        # data_json's archivedAt is always emitted as ISO 8601 UTC.
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def _make_snapshot(
    *,
    user_id: str,
    channel_id: str,
    video_id: str,
    field: str,
    value: Any,
    r2_key: Optional[str],
    captured_at: datetime,
    last_seen_at: datetime,
    superseded_at: datetime,
) -> VideoFieldSnapshot:
    return VideoFieldSnapshot(
        user_id=user_id,
        channel_id=channel_id,
        video_id=video_id,
        field=field,
        value_json=json.dumps(value),
        r2_key=r2_key,
        captured_at=captured_at,
        last_seen_at=last_seen_at,
        superseded_at=superseded_at,
    )


def _versioned_thumbnail_key(
    user_id: str, video_id: str, timestamp: datetime
) -> str:
    """Timestamped R2 key for a historical thumbnail snapshot.

    The "current" thumbnail lives at the canonical thumb_key path; each
    historical snapshot gets its own key with the supersession
    timestamp so we can show the whole history side by side.

    Uses the per-user prefix layout (Phase C onward).
    """
    ts = timestamp.strftime("%Y%m%dT%H%M%SZ")
    return r2_paths.thumb_history_key(user_id, video_id, ts)


def rescan_videos_batch(
    db: Session,
    *,
    rows: List[UserChannelVideo],
    creds: Credentials,
    settings: Optional[Dict[str, Any]] = None,
    removal_sink: Optional[Dict[Tuple[str, str], List[str]]] = None,
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Rescan a batch of videos with a single YouTube Data API call.

    Cost: 1 quota unit per 50 videos passed in. The caller can pass any
    number of rows - we paginate at 50 internally via fetch_video_details.

    Returns: {video_id: {field: {"old": ..., "new": ...}}} for each row
    that had at least one detected change. Rows whose YouTube state
    matches what we have aren't in the map.

    Rows whose video is missing from the API response feed the shared
    two-strike removal detector above; the strike that confirms a removal
    reports itself as a pseudo-field ``removed`` in the change map and
    releases the archive-integrity email. No snapshot is written either way -
    there's no new value to version.

    Caller is responsible for db.commit(), and for resolving the OAuth
    credentials (via app.oauth_loader.load_user_credentials) before
    calling this. The one exception is a confirmed removal: we commit it
    ourselves before emailing, because telling a user a video is gone on the
    back of a write that might still roll back is worse than a redundant
    commit.
    """
    if not rows:
        return {}

    now = _now()

    by_video_id: Dict[str, UserChannelVideo] = {r.video_id: r for r in rows}
    items = fetch_video_details(creds, list(by_video_id.keys()))
    items_by_id: Dict[str, Dict[str, Any]] = {
        item["id"]: item for item in items if item.get("id")
    }

    # Empty-response guard. A batch that comes back with zero items is a
    # failed probe, not fifty simultaneous deletions - quota exhaustion and
    # transient API errors both look exactly like this. Bail without touching
    # a single row, and without bumping last_metadata_sync_at, so the next
    # cron tick retries the same batch.
    if not items_by_id:
        log.warning(
            "rescan: videos.list returned nothing for %d rows (%s/%s) - "
            "treating as a failed probe, marking nothing removed",
            len(by_video_id), rows[0].user_id, rows[0].channel_id,
        )
        return {}

    all_changes: Dict[str, Dict[str, Dict[str, Any]]] = {}
    # (user_id, channel_id) -> number of removals confirmed this batch.
    confirmed_removals: Dict[Tuple[str, str], List[str]] = {}

    for vid, row in by_video_id.items():
        api_item = items_by_id.get(vid)
        if api_item is None:
            # Missing from an otherwise-healthy response. That's a removal
            # signal, never a verdict on its own - hand it to the debouncer.
            confirmed, prev_status = _record_absence(row, now=now)
            if confirmed:
                key = (row.user_id, row.channel_id)
                confirmed_removals.setdefault(key, []).append(row.video_id)
                all_changes[vid] = {
                    "removed": {
                        "old": prev_status,
                        "new": "deleted_on_youtube",
                    }
                }
            else:
                log.info(
                    "rescan: video %s absent from API (unconfirmed strike)", vid
                )
            row.last_metadata_sync_at = now
            continue

        changes = _apply_api_item_to_row(
            db, row=row, api_item=api_item, now=now, settings=settings,
        )
        # Seen on YouTube this run: drop any absence strikes it had banked.
        _record_sighting(row)
        if changes:
            all_changes[vid] = changes
        row.last_metadata_sync_at = now

    if confirmed_removals:
        if removal_sink is None:
            notify_confirmed_removals(db, confirmed_removals)
        else:
            # The caller is walking one channel in batches of 50 and will
            # notify once when it's done. Mailing from here would send one
            # email per batch, so a channel with 120 due videos whose
            # removals land in different batches would get three separate
            # emails for what the user experiences as a single event.
            for key, vids in confirmed_removals.items():
                removal_sink.setdefault(key, []).extend(vids)

    return all_changes


def _record_absence(
    row: UserChannelVideo, *, now: datetime
) -> Tuple[bool, Optional[str]]:
    """Apply one absence strike to a row. Returns (confirmed, prior status),
    where confirmed is True only for the strike that actually flipped the row.
    Unparseable data_json is left alone - we won't rewrite a row we can't
    read, and we certainly won't declare its video gone."""
    try:
        data = json.loads(row.data_json)
    except json.JSONDecodeError:
        return False, None
    prev_status = data.get("status")
    confirmed = note_video_missing(data, now=now)
    row.data_json = json.dumps(data)
    return confirmed, prev_status


def _record_sighting(row: UserChannelVideo) -> None:
    """Reset removal bookkeeping for a row YouTube just handed back to us.
    Writes only when there's something to clear, so a routine rescan doesn't
    rewrite data_json for every unchanged video."""
    try:
        data = json.loads(row.data_json)
    except json.JSONDecodeError:
        return
    stale = (
        data.get("removalMissCount")
        or data.get("lastMissAt")
        or data.get("deletedOnYoutubeAt")
        or data.get("status") == "deleted_on_youtube"
    )
    if not stale:
        return
    if data.get("status") == "deleted_on_youtube":
        # Back from the dead: restore whichever state the archive dictates.
        data["status"] = "archived" if data.get("localPath") else "discovered"
    clear_removal_marks(data)
    row.data_json = json.dumps(data)


def notify_confirmed_removals(
    db: Session, removed: Dict[Tuple[str, str], List[str]]
) -> None:
    """Fire the archive-integrity email for each channel that had a removal
    confirmed this batch. Best-effort: a mail failure must never fail a
    rescan, and the row stays marked either way.

    Keyed to the video ids that were confirmed removed, not just how many, so
    a single-video removal can name the video in the mail and link straight
    to it. Multiple in one sweep stay channel-level - there is no one video
    to point at."""
    # Persist the marks before we claim anything about them.
    db.commit()

    for (user_id, channel_id), video_ids in removed.items():
        count = len(video_ids)
        if count <= 0:
            continue
        # Exactly one removal: resolve its title so the mail can name it.
        video_id = video_ids[0] if count == 1 else None
        video_title = None
        if video_id is not None:
            v_row = db.get(UserChannelVideo, (user_id, channel_id, video_id))
            if v_row is not None:
                try:
                    video_title = (
                        json.loads(v_row.data_json) or {}
                    ).get("title") or None
                except (json.JSONDecodeError, TypeError):
                    video_title = None
        channel_name = channel_id
        ch_row = db.get(UserChannel, (user_id, channel_id))
        if ch_row is not None:
            try:
                channel_name = (
                    json.loads(ch_row.data_json) or {}
                ).get("name") or channel_id
            except (json.JSONDecodeError, TypeError):
                pass
        try:
            from app import notify as notify_lib  # noqa: WPS433

            notify_lib.notify_video_deleted(
                db,
                user_id=user_id,
                channel_youtube_id=channel_id,
                channel_name=channel_name,
                count=count,
                video_id=video_id,
                video_title=video_title,
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "video-deleted notification failed for %s/%s",
                user_id, channel_id,
            )


# ---------------------------------------------------------------------------
# Enumeration-sourced upkeep.
#
# Most of our users never do web OAuth - they authenticate a channel through
# the desktop worker instead, so there are no Data API credentials to rescan
# with and the videos.list path above can't run for them at all. What we DO
# have from the server is the channel's /videos tab, which enumerates reliably
# from our box.
#
# That listing is a strictly weaker instrument than the API and it is used
# here for exactly one question: was this video still on the channel's public
# listing? Presence and absence, nothing else. The rules below are what keep
# it honest.
# ---------------------------------------------------------------------------

# Only this privacy tier could appear on the /videos tab at all.
_ENUMERATION_VISIBLE_PRIVACY = "public"

# When we last saw this exact video in a channel-tab listing. This is the
# proof _absence_is_evaluable requires; nothing else on the row can supply it.
_CHANNEL_TAB_SEEN_KEY = "lastSeenOnChannelTabAt"


def _enumeration_can_see(data: Dict[str, Any]) -> bool:
    """Could the channel's public /videos listing show this video at all?

    Necessary, NOT sufficient - see _absence_is_evaluable for the other half.

    Only for a video we positively know is public. Private, unlisted and
    members-only videos are invisible to enumeration by definition, so their
    absence carries exactly zero information - evaluating them would hand
    every private video in the archive an absence strike on every run and
    then email the user that their private videos had been deleted. Private
    videos are the majority of what we hold and the whole point of the
    product, so this is the rule the rest of the path is built around.

    Anything else is excluded too: an unrecognised tier (age_restricted,
    members_only), a missing privacy key, or a row we can't parse.
    """
    privacy = data.get("privacy")
    if not isinstance(privacy, str):
        return False
    return privacy.strip().lower() == _ENUMERATION_VISIBLE_PRIVACY


def _absence_is_evaluable(data: Dict[str, Any]) -> bool:
    """Have we ever actually observed this video in a channel-tab listing?

    Stored privacy says whether YouTube would show the video to a logged-out
    visitor. It does not say the /videos tab is where it would show it.
    Shorts live under /shorts and past livestreams under /streams, and
    fetch_channel_videos reads /videos deliberately and only - so a public
    Short is absent from every enumeration we will ever run, permanently, and
    nothing stored on the row tells us it is one. Worker-discovered videos
    come off the uploads playlist and carry the type "video" whatever they
    actually are, so the type field can't be trusted to catch them either.

    Inferring listability from privacy therefore gives a still-live public
    Short an absence strike every run, flips it to deleted_on_youtube on the
    second, and mails the user that a video sitting right there in front of
    them is unavailable. So we don't infer it: we require having seen the
    video on the tab at least once, and only then does a later absence mean
    anything. A video already gone before our first successful enumeration
    never earns that proof and is never reported - staying quiet about a real
    removal is the cost, and it is the correct side to be wrong on.
    """
    return _parse_iso_utc(data.get(_CHANNEL_TAB_SEEN_KEY)) is not None


def _mark_seen_on_channel_tab(row: UserChannelVideo, now: datetime) -> None:
    """Bank the proof _absence_is_evaluable asks for. Written only from the
    enumeration path - turning up in videos.list says nothing about the tab."""
    try:
        data = json.loads(row.data_json)
    except json.JSONDecodeError:
        return
    data[_CHANNEL_TAB_SEEN_KEY] = now.isoformat()
    row.data_json = json.dumps(data)


def enumeration_can_see_row(row: UserChannelVideo) -> bool:
    """``_enumeration_can_see`` for a stored row: the candidate filter callers
    use to decide whether enumerating is worth a request at all. Rows that
    pass may still be ineligible for an absence strike (_absence_is_evaluable),
    but they have to be handed to the reconciler regardless so that the ones
    that ARE listed can bank their proof. Unparseable data_json is excluded -
    we won't reason about the visibility of a row we can't read."""
    try:
        data = json.loads(row.data_json)
    except json.JSONDecodeError:
        return False
    return _enumeration_can_see(data)


def reconcile_against_enumeration(
    db: Session,
    *,
    rows: List[UserChannelVideo],
    present_video_ids: Iterable[str],
    now: Optional[datetime] = None,
    removal_sink: Optional[Dict[Tuple[str, str], List[str]]] = None,
) -> Dict[str, int]:
    """Check stored rows against one successful channel-tab enumeration.

    ``present_video_ids`` is the id set from a listing the caller has ALREADY
    established was a real result. A failed or empty enumeration must never
    reach this function: absence only means something if the probe worked, and
    the caller owns that judgement (same contract as the empty-response guard
    on the API path above).

    Absence feeds the same two-strike debounce and the same notification
    accumulator as the OAuth path - there is one removal detector in this
    codebase and this is it. Presence clears banked strikes, and banks the
    proof that this video is the kind the tab lists at all: a row that has
    never been seen on the tab is checked but never struck, because a public
    Short is absent from /videos forever and being absent forever is not
    evidence of anything. See _absence_is_evaluable.

    Deliberately does NOT refresh any metadata. Flat tab entries are a thinner
    projection of a video than videos.list returns: the description is
    truncated or absent, viewCount comes back 0, uploadDate is a day-precision
    approximation derived from "3 weeks ago" text. Writing those over stored
    values would degrade the archive, and snapshotting the difference would
    write version history asserting the creator changed a field when all that
    changed is which source we asked. We only record what we can stand behind,
    and from this source that is presence.

    Returns counters for the caller's log. Caller owns db.commit().
    """
    now = now or _now()
    counters = {
        "checked": 0,
        "invisible": 0,
        "seen": 0,
        "absent": 0,
        "unproven": 0,
        "confirmed_removed": 0,
    }
    if not rows:
        return counters

    present: Set[str] = {v for v in present_video_ids if v}
    confirmed_removals: Dict[Tuple[str, str], List[str]] = {}

    for row in rows:
        try:
            data = json.loads(row.data_json)
        except json.JSONDecodeError:
            continue
        if not _enumeration_can_see(data):
            counters["invisible"] += 1
            continue
        counters["checked"] += 1
        # The parse above answers eligibility only; the mutation goes through
        # the same two helpers the API path uses so both paths write the
        # removal bookkeeping identically.
        if row.video_id in present:
            _record_sighting(row)
            _mark_seen_on_channel_tab(row, now)
            counters["seen"] += 1
        elif not _absence_is_evaluable(data):
            # Public, absent, and we have never once seen it on this tab -
            # so most likely it is a Short or a past livestream, which the
            # /videos tab does not carry and never will. Nothing to conclude.
            # last_metadata_sync_at still advances below: we did run the check,
            # and leaving it stale would re-enumerate this channel every night
            # forever for a row that can never produce an answer.
            counters["unproven"] += 1
            log.info(
                "enumeration: video %s absent but never seen on the channel "
                "tab (Short, livestream or tab-invisible) - not evaluated",
                row.video_id,
            )
        else:
            counters["absent"] += 1
            confirmed, prev_status = _record_absence(row, now=now)
            if confirmed:
                key = (row.user_id, row.channel_id)
                confirmed_removals.setdefault(key, []).append(row.video_id)
                counters["confirmed_removed"] += 1
                log.info(
                    "enumeration: video %s confirmed removed (was %s)",
                    row.video_id, prev_status,
                )
            else:
                log.info(
                    "enumeration: video %s absent from the channel tab "
                    "(unconfirmed strike)",
                    row.video_id,
                )
        row.last_metadata_sync_at = now

    if confirmed_removals:
        if removal_sink is None:
            notify_confirmed_removals(db, confirmed_removals)
        else:
            for key, vids in confirmed_removals.items():
                removal_sink.setdefault(key, []).extend(vids)

    return counters


def rescan_video_metadata(
    db: Session,
    *,
    row: UserChannelVideo,
    creds: Credentials,
) -> Dict[str, Dict[str, Any]]:
    """Single-video convenience wrapper around rescan_videos_batch.

    Returns the change dict for the one row (empty if no changes).

    Note: a one-row batch can never trip the removal detector - an empty
    response is indistinguishable from a failed probe at that size, so the
    empty-response guard swallows it. Removal detection needs the batch path.
    """
    all_changes = rescan_videos_batch(db, rows=[row], creds=creds)
    return all_changes.get(row.video_id, {})


# The three tiers the Data API and the worker spell identically. Anything
# outside this set cannot be compared across sources.
_API_COMPARABLE_PRIVACY = frozenset({"public", "unlisted", "private"})

# Every tier we actually store, including the one the Data API cannot see.
# Used to tell "we have no reading yet" apart from "we have one the API
# cannot express", which are handled very differently.
_STORED_PRIVACY_TIERS = _API_COMPARABLE_PRIVACY | {"members"}


def _api_privacy_is_comparable(old_value: Any, new_value: Any) -> bool:
    """Can a privacy difference between our stored value and a Data API
    reading be attributed to the creator?

    Only when both sides are tiers the two sources name the same way.

    Member-gating is a second axis that both sources flatten into this one
    field, and the Data API cannot see it at all: a members-only video
    reports privacyStatus "public". So a row the worker stored as "members"
    would version to "public" on the next API rescan, claiming the creator
    un-gated it, and back to "members" after the following worker refresh.
    One false history entry per pass, forever, describing an edit that never
    happened. A missing reading (None) is likewise not a change.

    The cost is a real gap: a creator genuinely turning member-gating on or
    off is not recorded. That is the correct side to be wrong on - a gap is
    silence, a false entry is a lie about the user in their own archive.
    """
    if new_value is None:
        return False
    return old_value in _API_COMPARABLE_PRIVACY and new_value in _API_COMPARABLE_PRIVACY


def _apply_api_item_to_row(
    db: Session,
    *,
    row: UserChannelVideo,
    api_item: Dict[str, Any],
    now: datetime,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Diff a pre-fetched API item against the row's stored state and
    write snapshots for any changed versioned fields.

    Mutates: row.data_json (only if there are changes), row.thumbnail_*
    columns (for thumbnail changes). Returns a dict of changes for the
    caller to log / report. Does NOT bump last_metadata_sync_at - that's
    the batch wrapper's responsibility so it stays consistent across the
    "no API result" and "API result, no change" cases.
    """
    api_values = _extract_from_api(api_item)
    data = json.loads(row.data_json)
    archived_at = _archived_at_iso_to_dt(data.get("archivedAt")) or now
    last_sync = row.last_metadata_sync_at or archived_at

    # We have never read this video's metadata before, so there is no prior
    # observation for anything to have changed FROM. Every value is a first
    # capture: store it, report nothing, and write no history.
    #
    # Without this the first run writes a snapshot per field saying the value
    # changed from null or "" to its real content, which renders to the user
    # as "you wrote this description on 21 Jul" for text that was there all
    # along. It fired on real data: the first metadata sweep wrote 9
    # description snapshots whose recorded old value was null or empty, plus
    # 3 viewCount snapshots claiming the count rose from zero.
    #
    # A genuinely empty description is still versioned normally on every
    # later run - this only suppresses the transition out of "never looked".
    first_observation = row.last_metadata_sync_at is None
    # Set when a first capture writes a value. `changes` deliberately stays
    # empty in that case, so it cannot be the thing that decides whether
    # data_json gets persisted.
    seeded = False

    changes: Dict[str, Dict[str, Any]] = {}

    # Scalar/list fields: title, description, tags, privacy. data_json
    # keys mirror these exactly (privacy is also `privacy` in the
    # frontend Video type). We always refresh the current value; the
    # snapshot (history) is only written when that field's history flag is
    # on. title/privacy aren't user-toggleable, so they always version.
    for field in ("title", "description", "tags", "privacy"):
        old_value = data.get(field)
        new_value = api_values[field]
        if _values_equal(field, old_value, new_value):
            continue
        if first_observation:
            # Seed only. privacy still defers to the comparability rule below
            # via its own None check, so an unknown reading cannot be seeded.
            if not (field == "privacy" and new_value is None):
                data[field] = new_value
                seeded = True
            continue
        if field == "privacy" and not _api_privacy_is_comparable(
            old_value, new_value
        ):
            # The two sources describe privacy differently, so a difference
            # here is not evidence the creator changed anything. See the
            # helper. One exception: a row with no recognised privacy yet has
            # nothing to supersede, so seed it rather than leaving it blank
            # forever. That is a first reading, not a change, so it is filled
            # in without a snapshot and without being reported as a change.
            # Checked against every tier we store, NOT the comparable three:
            # "members" is a real stored value and seeding over it is the very
            # overwrite this guard exists to prevent.
            if new_value is not None and old_value not in _STORED_PRIVACY_TIERS:
                data[field] = new_value
            continue
        flag = _FIELD_HISTORY_FLAG.get(field)
        if flag is None or _history_enabled(settings, flag):
            captured_at = _captured_at_for_previous_value(
                db,
                user_id=row.user_id,
                video_id=row.video_id,
                field=field,
                fallback=archived_at,
            )
            db.add(
                _make_snapshot(
                    user_id=row.user_id,
                    channel_id=row.channel_id,
                    video_id=row.video_id,
                    field=field,
                    value=old_value,
                    r2_key=None,
                    captured_at=captured_at,
                    last_seen_at=last_sync,
                    superseded_at=now,
                )
            )
        data[field] = new_value
        changes[field] = {"old": old_value, "new": new_value}

    # View count (stats). Only tracked when the user captures view count,
    # and only versioned into a time-series when they've opted into its
    # history. Because views change on nearly every rescan, an enabled
    # history flag yields one snapshot per rescan cadence — the graph.
    if settings is None or settings.get("saveViewCount", True):
        new_views = api_values.get("viewCount")
        old_views = _parse_view_count(data.get("viewCount"))
        if new_views is not None and old_views != new_views:
            # Same first-capture rule as the scalar fields above: the stored 0
            # is a placeholder from discovery, not a count we ever observed, so
            # the first real reading is not "views went up".
            if (
                not first_observation
                and old_views is not None
                and _history_enabled(settings, "saveViewCountHistory")
            ):
                captured_at = _captured_at_for_previous_value(
                    db,
                    user_id=row.user_id,
                    video_id=row.video_id,
                    field="viewCount",
                    fallback=archived_at,
                )
                db.add(
                    _make_snapshot(
                        user_id=row.user_id,
                        channel_id=row.channel_id,
                        video_id=row.video_id,
                        field="viewCount",
                        value=old_views,
                        r2_key=None,
                        captured_at=captured_at,
                        last_seen_at=last_sync,
                        superseded_at=now,
                    )
                )
            data["viewCount"] = new_views
            if first_observation:
                # Seeded, not observed changing. Reported as a change it would
                # still surface to the caller as an edit and get logged as one.
                seeded = True
            else:
                changes["viewCount"] = {"old": old_views, "new": new_views}

    # Thumbnail: content-hash dedup so we only store actual changes. Old
    # bytes are preserved as history only when thumbnail history is on.
    new_thumb_url = api_values["thumbnail_url"]
    old_thumb_url = data.get("thumbnailUrl")
    thumb_outcome = _rescan_thumbnail_if_changed(
        db,
        row=row,
        new_thumb_url=new_thumb_url,
        old_thumb_url=old_thumb_url,
        archived_at=archived_at,
        last_sync=last_sync,
        now=now,
        save_history=(
            not first_observation
            and _history_enabled(settings, "saveThumbnailHistory")
        ),
    )
    if thumb_outcome is not None:
        data["thumbnailUrl"] = new_thumb_url
        changes["thumbnail"] = thumb_outcome

    if changes or seeded:
        row.data_json = json.dumps(data)
    return changes


# ---------------------------------------------------------------------------
# Thumbnail-change detection.
#
# YouTube's thumbnail URL path stays stable across creator edits (only the
# `sqp` cache-buster query param changes), so URL comparison is unreliable
# in both directions:
#   - False negatives: a real thumbnail edit doesn't change the URL path
#   - False positives: routine CDN cache-busts make the URL look different
#
# Authoritative answer comes from comparing the bytes themselves via
# SHA-256. To avoid downloading bytes on every rescan when nothing has
# changed, we do a cheap HEAD request first and short-circuit when the
# etag or content-length match what we cached last time.
#
# Possible outcomes per call:
#   - HEAD says "matches cached" -> no GET, no R2 ops, return None.
#   - HEAD differs but downloaded bytes hash to the same SHA -> we update
#     the cached etag/content-length so next HEAD can short-circuit, no
#     R2 write, no snapshot, return None.
#   - SHA differs -> real change: snapshot old, write new bytes, update
#     all the cached fields, return the change descriptor.
# ---------------------------------------------------------------------------


def _head_signature(url: str) -> Tuple[Optional[str], Optional[int]]:
    """Cheap HEAD request to get (etag, content_length). Both can be None
    if the response doesn't include the header or the request fails -
    callers tolerate that and fall through to the GET."""
    try:
        resp = requests.head(url, timeout=5, allow_redirects=True)
        if resp.status_code >= 400:
            return None, None
        etag = resp.headers.get("etag")
        cl = resp.headers.get("content-length")
        try:
            cl_int = int(cl) if cl is not None else None
        except ValueError:
            cl_int = None
        return etag, cl_int
    except requests.RequestException:
        return None, None


def _rescan_thumbnail_if_changed(
    db: Session,
    *,
    row: UserChannelVideo,
    new_thumb_url: Optional[str],
    old_thumb_url: Optional[str],
    archived_at: datetime,
    last_sync: datetime,
    now: datetime,
    save_history: bool = True,
) -> Optional[Dict[str, Any]]:
    """Returns a change descriptor if the thumbnail actually changed
    (real new bytes), else None.

    ``save_history``: when True (default), the superseded thumbnail bytes are
    copied to a versioned history key and a snapshot row is written. When
    False, the current thumbnail is still refreshed but the old bytes are
    dropped (no history copy, no snapshot)."""
    if not new_thumb_url:
        return None

    # Step 1: HEAD-based short-circuit. If etag OR content-length match
    # what we stored on last rescan, the bytes haven't changed - skip
    # the download entirely.
    new_etag, new_cl = _head_signature(new_thumb_url)
    cached_etag = row.thumbnail_etag
    cached_cl = row.thumbnail_content_length
    if cached_etag and new_etag and cached_etag == new_etag:
        # Same image; nothing to do. The URL string may have changed
        # (cache-buster), but the bytes are byte-identical.
        return None
    if (
        cached_cl is not None
        and new_cl is not None
        and cached_cl == new_cl
        and cached_etag is None  # only trust length alone when we lack an etag baseline
    ):
        return None

    # Step 2: download new bytes (still cheaper than always-snapshot)
    # and hash them. SHA-256 is the source of truth.
    try:
        resp = requests.get(new_thumb_url, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning(
            "rescan: failed to fetch new thumbnail for %s: %s",
            row.video_id, e,
        )
        return None
    new_bytes = resp.content
    new_sha = hashlib.sha256(new_bytes).hexdigest()
    cached_sha = row.thumbnail_sha256

    # Bootstrap: first-ever rescan for this video has no cached_sha
    # baseline. Naively treating that as a "change" would create a
    # phantom snapshot for every video on first run. Read the existing
    # archived thumbnail bytes from R2 and hash THEM as the baseline
    # so we only snapshot when the image has actually been edited
    # since archive time.
    if cached_sha is None and row.thumbnail_r2_key:
        client = r2.client()
        bucket = r2.bucket()
        if client and bucket:
            try:
                obj = client.get_object(Bucket=bucket, Key=row.thumbnail_r2_key)
                existing_bytes = obj["Body"].read()
                cached_sha = hashlib.sha256(existing_bytes).hexdigest()
                # Persist the baseline so subsequent rescans short-circuit
                # via cached_etag/content_length without re-reading R2.
                row.thumbnail_sha256 = cached_sha
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "rescan: failed to baseline thumbnail sha for %s: %s",
                    row.video_id, e,
                )

    if cached_sha and cached_sha == new_sha:
        # False alarm: HEAD looked different but the actual bytes are
        # identical. This can happen when YouTube's CDN returns
        # different etags for the same content across edges. Refresh
        # the cached headers so next HEAD can short-circuit cleanly,
        # but DON'T snapshot or rewrite R2.
        row.thumbnail_etag = new_etag
        row.thumbnail_content_length = new_cl if new_cl is not None else len(new_bytes)
        return None

    # Step 3: real change. Write the new bytes to the canonical slot. When
    # history is on, first copy the old bytes to a versioned history key and
    # snapshot the old state; when off, the old bytes are simply superseded.
    client = r2.client()
    bucket = r2.bucket()
    history_key = _versioned_thumbnail_key(row.user_id, row.video_id, now)
    old_r2_key = row.thumbnail_r2_key
    copy_ok = False
    if save_history and old_r2_key and client and bucket:
        try:
            client.copy_object(
                Bucket=bucket,
                Key=history_key,
                CopySource={"Bucket": bucket, "Key": old_r2_key},
            )
            copy_ok = True
        except Exception as e:
            log.warning(
                "rescan thumb history copy failed for %s: %s",
                row.video_id, e,
            )
            history_key = old_r2_key  # snapshot still references the original location

    canonical_key = r2_paths.thumb_key(row.user_id, row.video_id)
    put_ok = False
    if client and bucket:
        try:
            client.put_object(
                Bucket=bucket,
                Key=canonical_key,
                Body=new_bytes,
                ContentType=resp.headers.get("Content-Type", "image/jpeg")
                .split(";")[0]
                .strip(),
            )
            put_ok = True
        except Exception as e:
            log.warning(
                "rescan: failed to upload new thumbnail to R2 for %s: %s",
                row.video_id, e,
            )

    # Update the storage ledger to reflect the overwrite. Runs once the new
    # bytes have landed AND we're not in the history-on-but-copy-failed
    # partial-failure case (that's left for reconciliation, preserving the
    # old bytes). keep_history mirrors whether we actually preserved the old
    # bytes at the history key.
    if put_ok and (copy_ok or not save_history):
        storage_ledger.rotate_in_place(
            db,
            user_id=row.user_id,
            r2_key=canonical_key,
            new_history_key=history_key,
            new_bytes=len(new_bytes),
            kind="thumbnail",
            history_kind="snapshot",
            rotated_at=now,
            new_metadata_bytes=r2.metadata_bytes_for(content_type="image/jpeg"),
            keep_history=copy_ok,
        )
        # Cross-layout cleanup: if the row was pointing at an OLD-layout
        # thumbnail key (e.g. ``thumbnails/{vid}.jpg``) and we've now
        # rotated to the NEW per-user-prefix layout, the OLD R2 object
        # is no longer referenced (its bytes are either preserved at the
        # history key or, with history off, intentionally dropped). Delete
        # it from R2 + close out the ledger row so we stop paying for a
        # stale copy. Never delete the history copy itself.
        if (
            old_r2_key
            and old_r2_key != canonical_key
            and old_r2_key != history_key
            and client
            and bucket
        ):
            try:
                client.delete_object(Bucket=bucket, Key=old_r2_key)
                storage_ledger.mark_deleted(db, [old_r2_key], deleted_at=now)
            except Exception as e:
                log.warning(
                    "rescan: failed to remove obsolete old-layout thumbnail %s: %s",
                    old_r2_key, e,
                )

    # Only record the history snapshot when we actually preserved the old
    # bytes (copy_ok). If the history copy failed, history_key points at the
    # canonical slot we just overwrote with the NEW image, so a snapshot
    # there would misrepresent the old value — skip it.
    if save_history and copy_ok:
        captured_at = _captured_at_for_previous_value(
            db,
            user_id=row.user_id,
            video_id=row.video_id,
            field="thumbnail",
            fallback=archived_at,
        )
        db.add(
            _make_snapshot(
                user_id=row.user_id,
                channel_id=row.channel_id,
                video_id=row.video_id,
                field="thumbnail",
                value={
                    "url": old_thumb_url,
                    "sha256": cached_sha,
                },
                r2_key=history_key,
                captured_at=captured_at,
                last_seen_at=last_sync,
                superseded_at=now,
            )
        )

    # Refresh all cached fields with the new image's signatures so the
    # next rescan's HEAD short-circuit works.
    row.thumbnail_r2_key = canonical_key
    row.thumbnail_size_bytes = len(new_bytes)
    row.thumbnail_sha256 = new_sha
    row.thumbnail_etag = new_etag
    row.thumbnail_content_length = new_cl if new_cl is not None else len(new_bytes)

    return {
        "old": {"url": old_thumb_url, "r2_key": history_key, "sha256": cached_sha},
        "new": {"url": new_thumb_url, "r2_key": canonical_key, "sha256": new_sha},
    }
