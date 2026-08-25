from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Cookie, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    Channel,
    ChannelOwnership,
    SyncJob,
    User,
    UserChannel,
    UserChannelSubscription,
    UserChannelVideo,
    UserGoogleConnection,
    UserSession,
    UserYouTubeSettings,
    Video,
    VideoComment,
    VideoFieldSnapshot,
    WorkerYoutubeConnection,
)
from app import access
from app.security import SESSION_COOKIE_NAME, get_current_user, get_paid_user
from app.service_access import service_is_active
from app import comments_rescan, email as email_lib, encryption, google_oauth, metadata_rescan, pubsub, r2, r2_paths, storage_ledger
from app.metadata_rescan import (
    clear_removal_marks,
    enumeration_can_see_row,
    note_video_missing,
)
from app.youtube_scrape import (
    fetch_channel_about,
    fetch_channel_avatar_url,
    fetch_channel_handle,
    fetch_channel_stats,
    fetch_channel_videos,
    fetch_video_metadata,
    fetch_video_visibility_detailed,
    resolve_channel_id,
)

log = logging.getLogger("archive336.youtube")

router = APIRouter()


# Hosts we know serve YouTube's own images. Checked first so nothing about
# the storage heuristics below can ever misfire on a legitimate CDN url.
_YOUTUBE_CDN_HOST_SUFFIXES = (
    "ggpht.com",
    "googleusercontent.com",
    "ytimg.com",
    "youtube.com",
)


def _is_own_storage_url(url: Any) -> bool:
    """True when ``url`` points back into our own object storage.

    Our read paths hand the frontend a *presigned* url for the archived
    avatar, and the frontend PUTs the whole channel payload back on save.
    Persisting that value poisons the row: the signature expires within the
    hour, and the real YouTube CDN url - our only way to refetch the bytes -
    is gone for good. So anything matching this is refused at every write
    boundary.

    Signals, any one is enough:
      - an ``X-Amz-Signature`` query parameter (presigned; only our own
        storage hands us one),
      - the host of the configured storage endpoint (STORAGE_* wins over the
        legacy R2_* names, same precedence as r2._load),
      - the egress proxy host we rewrite large downloads through,
      - the bucket name appearing as the host prefix or first path segment.
    """
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if not host:
        return False
    if any(
        host == suffix or host.endswith("." + suffix)
        for suffix in _YOUTUBE_CDN_HOST_SUFFIXES
    ):
        return False

    if "x-amz-signature" in (parts.query or "").lower():
        return True

    endpoint = os.environ.get("STORAGE_ENDPOINT") or os.environ.get("R2_ENDPOINT")
    if endpoint:
        endpoint_host = (urlsplit(endpoint).hostname or "").lower()
        if endpoint_host and (
            host == endpoint_host or host.endswith("." + endpoint_host)
        ):
            return True

    proxy_host = (os.environ.get("STORAGE_PROXY_HOST") or "").strip().lower()
    if proxy_host and host == proxy_host:
        return True

    bucket = (
        os.environ.get("STORAGE_BUCKET") or os.environ.get("R2_BUCKET") or ""
    ).strip().lower()
    if bucket:
        path_head = (parts.path or "").lstrip("/").split("/", 1)[0].lower()
        if host.startswith(bucket + ".") or path_head == bucket:
            return True

    return False


def _archive_channel_avatar(
    db: Session, row: UserChannel, payload: Dict[str, Any]
) -> None:
    """Copy the channel's YouTube CDN avatar bytes to storage at
    ``r2_paths.avatar_key`` and stamp ``row.avatar_r2_key`` on success.

    Best-effort: failures are logged and the row is left untouched, so
    the response serializer falls back to the YouTube URL stored in
    data_json. Skips if no real avatar URL is present, if it's still
    a picsum placeholder, or if we already have a key recorded.

    Records the upload in the storage ledger so the bill cron sees it.
    """
    if row.avatar_r2_key:
        return
    avatar_url = (payload.get("avatarUrl") or "").strip()
    if not avatar_url or "picsum.photos" in avatar_url:
        return
    key = r2_paths.avatar_key(row.user_id, row.channel_id)
    try:
        size, metadata_bytes = r2.download_to_r2(
            avatar_url, key, "image/jpeg", subject=row.user_id
        )
        row.avatar_r2_key = key
        storage_ledger.record_object(
            db,
            user_id=row.user_id,
            r2_key=key,
            byte_count=size,
            kind="avatar",
            metadata_bytes=metadata_bytes,
        )
        # Mirror to the shared-pool Channel so the new-model read
        # routes can serve the archived avatar without rejoining
        # against legacy UserChannel.
        from app.models import Channel as _Channel  # noqa: WPS433

        new_channel = (
            db.query(_Channel)
            .filter(_Channel.youtube_id == row.channel_id)
            .one_or_none()
        )
        if new_channel is not None:
            # Unconditional overwrite. This used to only fill the column when
            # it was empty, which meant a value written under an older key
            # layout could never be corrected - and the read path presigns
            # THIS column, so a stale key 403s the whole channel's avatar and
            # renders as an empty circle with nothing logged. We only reach
            # this line after download_to_r2 returned, so ``key`` names bytes
            # that were just written and verified; there is no good value here
            # that it could clobber.
            new_channel.avatar_r2_key = key
    except Exception:  # noqa: BLE001
        log.exception(
            "failed to archive avatar for channel %s", row.channel_id
        )


def _archive_thumbnails_parallel(
    db: Session,
    jobs: List[Tuple[UserChannelVideo, str]],
    max_workers: int = 8,
) -> None:
    """Download each (row, thumbnail_url) pair to R2 at
    thumbnails/{video_id}.jpg and stamp the row's thumbnail_r2_key +
    thumbnail_size_bytes. Failures are logged + skipped (the video's
    YouTube CDN URL still works as a fallback at read time).

    Runs the HTTP fetches in a thread pool so a few hundred thumbnails
    don't add seconds of serial latency to the import endpoint.
    SQLAlchemy session writes happen in the calling thread only - the
    worker threads just download bytes. The storage ledger insert also
    happens in the calling thread for the same reason.
    """
    if not jobs:
        return

    futures: Dict[Any, Tuple[UserChannelVideo, str]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for row, url in jobs:
            key = r2_paths.thumb_key(row.user_id, row.video_id)
            fut = ex.submit(
                r2.download_to_r2, url, key, "image/jpeg", subject=row.user_id
            )
            futures[fut] = (row, key)

        for fut in as_completed(futures):
            row, key = futures[fut]
            try:
                size, metadata_bytes = fut.result()
                row.thumbnail_r2_key = key
                row.thumbnail_size_bytes = size
                storage_ledger.record_object(
                    db,
                    user_id=row.user_id,
                    r2_key=key,
                    byte_count=size,
                    kind="thumbnail",
                    metadata_bytes=metadata_bytes,
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "failed to archive thumbnail for video %s",
                    row.video_id,
                )


# ---------- Settings ----------


@router.get("/settings")
def get_settings(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Optional[Dict[str, Any]]:
    """Return the user's saved YouTube settings, or null if they've never saved."""
    row = db.get(UserYouTubeSettings, current.id)
    if row is None:
        return None
    try:
        return json.loads(row.settings_json)
    except json.JSONDecodeError:
        # If the stored blob is somehow corrupt, treat as missing rather than 500.
        return None


@router.put("/settings", status_code=status.HTTP_204_NO_CONTENT)
def put_settings(
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Response:
    """Upsert the user's YouTube settings. Frontend owns the schema."""
    blob = json.dumps(payload)
    row = db.get(UserYouTubeSettings, current.id)
    if row is None:
        row = UserYouTubeSettings(user_id=current.id, settings_json=blob)
        db.add(row)
    else:
        row.settings_json = blob
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------- Channels ----------


def _resolve_avatar(
    payload: Dict[str, Any],
    channel_id: str,
    existing: Optional[Dict[str, Any]] = None,
) -> None:
    """Ensure payload['avatarUrl'] matches the saveChannelAvatar toggle.

    - Toggle OFF → clear the stored URL (no placeholders, no stale data).
    - Toggle ON, real URL already stored → leave it alone.
    - Toggle ON, no real URL yet → fetch from YouTube. Raises 502 if unreachable.

    A url pointing at our own storage counts as "no real URL". The frontend
    receives a presigned url on read and PUTs it straight back, so without
    this the payload's own round trip would look like a valid avatar and we
    would skip the refetch forever - which is how the real YouTube CDN url
    got overwritten by an expired signature in the first place.
    """
    # Drop the round-tripped signature before anything reads it, so no caller
    # of this function can persist one downstream. Fall back to the CDN url we
    # already have on the row rather than straight to "": every settings save
    # PUTs the whole channel back, so blanking unconditionally would make the
    # Active toggle scrape YouTube on every click and 502 the save whenever
    # YouTube is slow. Only a row that is itself poisoned pays for a refetch,
    # and it pays once - the fetched CDN url is what gets stored.
    repairing = False
    if _is_own_storage_url(payload.get("avatarUrl")):
        prior = (existing or {}).get("avatarUrl")
        payload["avatarUrl"] = "" if _is_own_storage_url(prior) else (prior or "")
        # We blanked a poisoned value rather than the user clearing anything.
        # If both the payload and the row were poisoned we now need a refetch,
        # but that refetch is OUR cleanup, not something they asked for, so it
        # must not be allowed to fail their save. See the best-effort branch
        # at the bottom of this function.
        repairing = not payload["avatarUrl"]

    settings = payload.get("settings") or {}
    if not settings.get("saveChannelAvatar"):
        # Toggle off — preserve any existing avatar URL on the server.
        # The frontend hides the picture via its own toggle check; we
        # don't delete data we already have.
        return

    existing = (payload.get("avatarUrl") or "").strip()
    needs_fetch = not existing or "picsum.photos" in existing
    if not needs_fetch:
        return

    real = fetch_channel_avatar_url(channel_id)
    if not real:
        if repairing:
            # Self-inflicted refetch: the only reason this row has no url is
            # that we just discarded a presigned one of our own. Failing the
            # user's save over our own cleanup would surface a backend defect
            # as a broken settings page. Leave it empty, log it, and let the
            # next refresh pick it up - the archived avatar bytes are
            # unaffected either way.
            log.warning(
                "channel %s: discarded a round-tripped storage url and could "
                "not refetch the CDN url from YouTube; leaving it empty",
                channel_id,
            )
            payload["avatarUrl"] = ""
            return
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Couldn't reach YouTube to fetch the channel's profile picture.",
        )
    payload["avatarUrl"] = real


def _resolve_stats(
    payload: Dict[str, Any],
    channel_id: str,
    existing: Optional[Dict[str, Any]] = None,
) -> None:
    """Ensure live-stats fields match the saveChannelStatsSnapshots toggle.

    Refreshes stats only when the toggle transitions from off → on at save
    time (or on initial add — `existing` is None for POST). Avoids stat
    fetches on every settings save while the toggle is already on.

    - Toggle OFF → preserve existing counts on the server (frontend hides).
    - Toggle ON, was already on → leave existing values alone.
    - Toggle ON, was off (or first time) → fetch from YouTube. 502 if unreachable.
    """
    settings = payload.get("settings") or {}
    if not settings.get("saveChannelStatsSnapshots"):
        # Toggle off — keep whatever stats we already have. The frontend
        # hides the stats row via its own toggle check.
        return

    was_on = False
    if existing is not None:
        prev_settings = existing.get("settings") or {}
        was_on = bool(prev_settings.get("saveChannelStatsSnapshots"))
    if was_on:
        # Toggle hasn't transitioned — keep whatever we already had.
        return

    stats = fetch_channel_stats(channel_id)
    if stats is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Couldn't reach YouTube to fetch live stats.",
        )
    payload["subscriberCount"] = stats["subscriberCount"]
    payload["videoCount"] = stats["videoCount"]
    payload["totalViews"] = stats["totalViews"]


_VIDEO_FIELD_MAP: Dict[str, "tuple[str, Any]"] = {
    "saveThumbnail": ("thumbnailUrl", ""),
    "saveViewCount": ("viewCount", 0),
    "saveDescription": ("description", ""),
    "saveTags": ("tags", []),
    # comments / captions: not yet implemented (deferred to the very end)
}


def _capture_at_discovery(settings: Dict[str, Any]) -> Dict[str, bool]:
    """Which metadata fields to capture when a NEW video first appears.

    Two layers of gating:
      1. Master toggle includeMetadataOnVideoSync. When False, nothing
         optional gets captured at discovery time - just the bare
         identity (title, duration, upload date). The scheduled metadata
         rescan picks up the rest later.
      2. Per-field toggles (saveThumbnail / saveViewCount /
         saveDescription / saveTags). When master is True but one of
         these is False, that field is skipped individually.

    Defaults are all True to preserve the pre-gate behavior for users
    who haven't touched their settings.

    Returned dict keys mirror the data_json field names so the caller
    can iterate.
    """
    master = settings.get("includeMetadataOnVideoSync", True)
    if not master:
        return {
            "thumbnailUrl": False,
            "viewCount": False,
            "description": False,
            "tags": False,
        }
    return {
        "thumbnailUrl": bool(settings.get("saveThumbnail", True)),
        "viewCount": bool(settings.get("saveViewCount", True)),
        "description": bool(settings.get("saveDescription", True)),
        "tags": bool(settings.get("saveTags", True)),
    }


def _apply_discovery_gate(
    video_payload: Dict[str, Any],
    existing_video: Dict[str, Any],
    want: Dict[str, bool],
) -> None:
    """Filter a freshly-built video_payload based on what the user wants
    captured at discovery time.

    Mutates video_payload in place. Preserves anything already on the
    existing row (we never DELETE data on toggle changes - the user
    paid to capture it the first time). Only blanks fields when:
      - the toggle is currently OFF, AND
      - there's no existing value to preserve.

    This matches the behavior of _apply_video_metadata_toggles: settings
    are forward-going, never destructive of prior data.
    """
    defaults: Dict[str, Any] = {
        "thumbnailUrl": "",
        "viewCount": 0,
        "description": "",
        "tags": [],
    }
    for field, capture in want.items():
        if capture:
            continue
        existing_value = existing_video.get(field)
        if existing_value:
            # Preserve what we already had.
            video_payload[field] = existing_value
        else:
            # Nothing prior - leave the empty default.
            video_payload[field] = defaults[field]


def _apply_video_metadata_toggles(
    db: Session,
    user_id: str,
    channel_id: str,
    settings: Dict[str, Any],
    prev_settings: Optional[Dict[str, Any]],
) -> None:
    """Reconcile each video record with the channel's per-field metadata toggles.

    For each toggle (saveThumbnail / saveViewCount / saveDescription / saveTags):
      - off → on (PUT) → fetch the watch page and populate.
      - any other case → no-op.

    Crucially, toggling a field OFF does NOT delete what we already have.
    The toggle is a per-user display preference; the data persists on the
    server so other users tracking the same channel can still benefit, and
    so flipping the toggle back on doesn't trigger a re-scrape.

    Watch-page fetches are batched: one call per video covers all queued
    field-fetches at once.
    """
    fields_to_fetch: set = set()

    for toggle_key, (video_key, _empty_val) in _VIDEO_FIELD_MAP.items():
        is_on = bool(settings.get(toggle_key))
        was_on = bool(prev_settings.get(toggle_key)) if prev_settings is not None else None
        # Only fetch on off → on transition.
        if is_on and was_on is False:
            fields_to_fetch.add(video_key)

    if not fields_to_fetch:
        return

    rows = (
        db.query(UserChannelVideo)
        .filter(
            UserChannelVideo.user_id == user_id,
            UserChannelVideo.channel_id == channel_id,
        )
        .all()
    )
    for r in rows:
        try:
            data = json.loads(r.data_json)
        except json.JSONDecodeError:
            continue

        meta = fetch_video_metadata(r.video_id)
        if meta is None:
            continue
        # Only write fields the scrape actually came back with. A watch page
        # that loaded but told us nothing - the bot interstitial being the
        # common case - parses into empty strings, 0 and [], and writing those
        # would blank a description we already hold and report a real video as
        # having no views. An empty scrape is an absent answer, not a new one.
        # The cost is that a genuine 0-view video keeps whatever it had, which
        # is 0 anyway on any row we created.
        if "thumbnailUrl" in fields_to_fetch and meta.get("thumbnailUrl"):
            data["thumbnailUrl"] = meta["thumbnailUrl"]
        if "viewCount" in fields_to_fetch and meta.get("viewCount"):
            data["viewCount"] = meta["viewCount"]
        if "description" in fields_to_fetch and meta.get("description"):
            data["description"] = meta["description"]
        if "tags" in fields_to_fetch and meta.get("tags"):
            data["tags"] = meta["tags"]

        r.data_json = json.dumps(data)


def _video_template(channel_id: str, v: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap a freshly scraped video dict with the full Video shape the frontend expects."""
    return {
        **v,
        "channelId": channel_id,
        "status": "discovered",
        "privacy": "public",
        "type": "video",
        "tags": [],
        "commentCount": 0,
        "comments": [],
        "captionLanguages": [],
        "videoFormat": None,
        "videoResolution": None,
        "videoBitrateKbps": None,
        "localPath": None,
        "fileSizeBytes": None,
        "firstSeenAt": v.get("uploadDate") or "",
        "archivedAt": None,
        "lastYoutubeCheckAt": None,
        "deletedOnYoutubeAt": None,
    }


# The only visibility verdicts we are willing to write down. Anything else -
# an "unknown" marker, a value the scraper grows later, a shape we don't
# recognise - means the probe did not establish what the video is.
#
# This matters because the watch-page probe is bot-checked most of the time
# from our own IPs, and YouTube serves that interstitial through the same
# playabilityStatus field a genuinely private video uses. An indeterminate
# probe that reaches the branches below writes a privacy flip the creator
# never made, or banks a removal strike that ends in an email telling a user
# their video is gone. "We could not look" has to stay distinguishable from
# "it is not there", and where it isn't, we say nothing.
_ACTIONABLE_VISIBILITY = ("public", "unlisted", "private", "members", "deleted")


def _visibility_probe_verdict(probe: Any) -> Tuple[Optional[str], str, str]:
    """Normalise whatever ``fetch_video_visibility_detailed`` handed back into
    ``(verdict, raw_status, raw_reason)``, with ``verdict`` None whenever the
    probe did not confidently establish a visibility.

    Deliberately tolerant about the container - tuple, bare string, mapping,
    object - and deliberately strict about the verdict. The scraper reports
    indeterminate probes and may gain fields or values over time; the failure
    mode of guessing here is a false "your video was deleted" email, so
    anything not on the recognised list comes back as "could not determine"
    and the caller leaves the row exactly as it found it.
    """
    if probe is None:
        return None, "", ""

    verdict: Any = None
    raw_status: Any = ""
    raw_reason: Any = ""
    if isinstance(probe, str):
        verdict = probe
    elif isinstance(probe, (tuple, list)):
        parts = list(probe) + ["", "", ""]
        verdict, raw_status, raw_reason = parts[0], parts[1], parts[2]
    elif isinstance(probe, dict):
        verdict = probe.get("verdict") or probe.get("visibility")
        raw_status = probe.get("raw_status") or probe.get("status") or ""
        raw_reason = probe.get("raw_reason") or probe.get("reason") or ""
    else:
        verdict = getattr(probe, "verdict", None) or getattr(
            probe, "visibility", None
        )
        raw_status = getattr(probe, "raw_status", "") or ""
        raw_reason = getattr(probe, "raw_reason", "") or ""

    raw_status = str(raw_status or "")
    raw_reason = str(raw_reason or "")
    if not isinstance(verdict, str) or verdict not in _ACTIONABLE_VISIBILITY:
        return None, raw_status, raw_reason
    return verdict, raw_status, raw_reason


def _full_sync_videos(
    db: Session,
    user_id: str,
    channel_id: str,
) -> Dict[str, int]:
    """Reconcile our stored videos with what's currently on YouTube.

    - New videos on YouTube → insert.
    - Existing videos still on YouTube → update title/description/views/etc.
    - Existing PUBLIC videos missing from the listing → fetch the watch page and
      re-classify as private/members/deleted (preserving the row - this is an
      archive, after all). Rows we already hold as private/unlisted/members are
      not evaluated at all: the listing was never going to show them.

    Raises 502 if the initial channel /videos fetch fails. Per-video classification
    failures (couldn't reach the watch page) are silent; those videos are left as-is.
    """
    fresh = fetch_channel_videos(channel_id)
    if fresh is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Couldn't reach YouTube to sync videos.",
        )

    fresh_by_id = {v["id"]: v for v in fresh}

    rows = (
        db.query(UserChannelVideo)
        .filter(
            UserChannelVideo.user_id == user_id,
            UserChannelVideo.channel_id == channel_id,
        )
        .all()
    )
    existing_by_id: Dict[str, UserChannelVideo] = {}
    for r in rows:
        existing_by_id[r.video_id] = r

    added = 0
    updated = 0
    privatized = 0
    deletedified = 0

    # New videos: insert
    for vid, v in fresh_by_id.items():
        if vid in existing_by_id:
            continue
        full = _video_template(channel_id, v)
        db.add(
            UserChannelVideo(
                user_id=user_id,
                channel_id=channel_id,
                video_id=vid,
                data_json=json.dumps(full),
            )
        )
        added += 1

    # Still-public videos: refresh title/description/view count/duration/upload date.
    # Preserve our local fields (status, archive paths, etc).
    for vid, v in fresh_by_id.items():
        if vid not in existing_by_id:
            continue
        row = existing_by_id[vid]
        try:
            data = json.loads(row.data_json)
        except json.JSONDecodeError:
            data = _video_template(channel_id, v)
        # Refresh the YouTube-sourced fields, keep our archival fields.
        # `or`, not a get-default: the listing sometimes carries an empty
        # title, and "the scrape gave us nothing" must not overwrite a title
        # we already hold. Same rule every field below already follows.
        data["title"] = v.get("title") or data.get("title", "")
        data["description"] = v.get("description") or data.get("description", "")
        data["uploadDate"] = v.get("uploadDate") or data.get("uploadDate", "")
        data["durationSec"] = v.get("durationSec") or data.get("durationSec", 0)
        data["thumbnailUrl"] = v.get("thumbnailUrl") or data.get("thumbnailUrl", "")
        data["viewCount"] = v.get("viewCount") or data.get("viewCount", 0)
        # If we previously marked it as deleted/private but it's back on the public
        # listing, clear those flags - including any banked absence strikes.
        if data.get("status") == "deleted_on_youtube":
            data["status"] = "discovered"
        clear_removal_marks(data)
        if data.get("privacy") in ("private", "members"):
            data["privacy"] = "public"
        row.data_json = json.dumps(data)
        updated += 1

    # Empty-response guard: the public /videos listing coming back with zero
    # entries while we hold rows for the channel means the scrape was blocked
    # or rate-limited far more often than it means the channel emptied itself.
    # Skip the whole re-classification pass rather than probe (and possibly
    # mis-classify) every video we have.
    if not fresh_by_id and existing_by_id:
        log.warning(
            "sync: public listing for %s returned nothing while %d rows exist "
            "- treating as a failed probe, re-classifying nothing",
            channel_id, len(existing_by_id),
        )
        db.commit()
        return {
            "added": added,
            "updated": updated,
            "privatized": privatized,
            "deletedified": deletedified,
        }

    # Missing videos: probe each watch page and (re-)classify.
    #
    # Only rows the public listing SHOULD have shown are evaluated. A private,
    # unlisted or members-only video is invisible to that listing by
    # definition, so its absence says nothing - and the watch page can't
    # supply the missing answer either: from our IPs it mostly returns the bot
    # interstitial, and the responses that aren't recognised land in the
    # "deleted" catch-all. Running note_video_missing over private rows is
    # exactly how a user ends up emailed that the private videos they still
    # have were deleted, and private videos are most of what we hold. Same
    # rule the nightly path runs on, deliberately sharing its one definition.
    #
    # Nothing real is lost: a video that went private for real is still stored
    # as public here so it is evaluated, and one that came back to public
    # reappears in fresh_by_id and heals in the refresh loop above.
    now = datetime.now(timezone.utc)
    for vid, row in existing_by_id.items():
        if vid in fresh_by_id:
            continue
        if not enumeration_can_see_row(row):
            continue
        try:
            data = json.loads(row.data_json)
        except json.JSONDecodeError:
            continue
        verdict, raw_status, raw_reason = _visibility_probe_verdict(
            fetch_video_visibility_detailed(vid)
        )
        if verdict is None:
            # We could not look, so we say nothing. Not a privacy change, not
            # a removal, not even a strike - the stored value stays exactly as
            # it was and this row is not rewritten. The bot interstitial lands
            # here, which is the whole reason this guard exists.
            log.info(
                "sync: %s visibility indeterminate "
                "(playabilityStatus=%s reason=%r) - leaving the row alone",
                vid, raw_status or "?", raw_reason,
            )
            continue
        prev_privacy = data.get("privacy")
        prev_status = data.get("status")
        # A visibility change says nothing about whether we hold the file, so
        # only a row we had marked deleted needs its status rewritten - and it
        # goes back to whichever state the archive dictates, the same way
        # _record_sighting and the OAuth reconciler restore theirs. A blanket
        # "discovered" here would drop an archived video out of the user's
        # archived count and re-queue it for a download we already paid for.
        if prev_status == "deleted_on_youtube":
            live_status = "archived" if data.get("localPath") else "discovered"
        else:
            live_status = prev_status or "discovered"
        if verdict == "public":
            # Still publicly accessible. Clear any flags we'd set previously.
            data["status"] = live_status
            data["privacy"] = "public"
            clear_removal_marks(data)
        elif verdict == "unlisted":
            data["status"] = live_status
            data["privacy"] = "unlisted"
            clear_removal_marks(data)
            if prev_privacy != "unlisted":
                privatized += 1
        elif verdict == "private":
            data["status"] = live_status
            data["privacy"] = "private"
            clear_removal_marks(data)
            if prev_privacy != "private":
                privatized += 1
        elif verdict == "members":
            data["status"] = live_status
            data["privacy"] = "members"
            clear_removal_marks(data)
            if prev_privacy != "members":
                privatized += 1
        else:  # "deleted" - the only recognised verdict left
            # The "deleted" verdict is the scraper's catch-all bucket, so one
            # hit is a signal, not proof. Debounce it exactly like the OAuth
            # path does and only count the strike that actually confirms.
            # YouTube's reason prose only ever goes to the log - it names a
            # cause we can't verify, and note_video_missing deliberately
            # refuses to persist it where a user could end up reading it.
            log.info(
                "sync: %s unavailable (playabilityStatus=%s reason=%r)",
                vid, raw_status, raw_reason,
            )
            if note_video_missing(
                data, now=now, evidence=(raw_status, raw_reason)
            ):
                deletedified += 1
        row.data_json = json.dumps(data)

    db.commit()

    _notify_videos_deleted(db, user_id, channel_id, deletedified)

    return {
        "added": added,
        "updated": updated,
        "privatized": privatized,
        "deletedified": deletedified,
    }


def _notify_videos_deleted(
    db: Session, user_id: str, channel_id: str, count: int
) -> None:
    """Archive-integrity alert: videos vanished from YouTube but we still hold
    the archived copies. Gated on the channel's notifyVideoDeleted toggle;
    best-effort so a mail hiccup never fails the sync.

    ``count`` must only ever be the number of CONFIRMED new transitions this
    run (see note_video_missing) - a strike that hasn't been confirmed yet
    must not reach here, or we'd email about a video that is probably fine.
    """
    if count <= 0:
        return
    try:
        from app import notify as notify_lib  # noqa: WPS433

        ch_row = db.get(UserChannel, (user_id, channel_id))
        ch_name = channel_id
        if ch_row is not None:
            try:
                ch_name = (
                    json.loads(ch_row.data_json) or {}
                ).get("name") or channel_id
            except (json.JSONDecodeError, TypeError):
                pass
        notify_lib.notify_video_deleted(
            db,
            user_id=user_id,
            channel_youtube_id=channel_id,
            channel_name=ch_name,
            count=count,
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "video-deleted notification failed for %s/%s",
            user_id, channel_id,
        )


def _resolve_about(payload: Dict[str, Any], channel_id: str) -> None:
    """Ensure About-tab fields match the saveChannelAbout toggle.

    Covers: name, description, country, joinedAt, links.

    - Toggle OFF → preserve existing data on the server (frontend hides).
    - Toggle ON, real data already stored → leave it alone.
    - Toggle ON, fields are empty/placeholder → fetch from YouTube.
      Raises 502 if YouTube is unreachable.
    """
    settings = payload.get("settings") or {}
    handle = payload.get("handle") or ""

    if not settings.get("saveChannelAbout"):
        # Toggle off — keep whatever About data we have on the server.
        # The frontend hides the About fields via its own toggle check.
        return

    # We treat "name == handle" or empty as the placeholder state.
    name = (payload.get("name") or "").strip()
    needs_fetch = (not name) or (name == handle)
    if not needs_fetch:
        return

    about = fetch_channel_about(channel_id)
    if about is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Couldn't reach YouTube to fetch the channel's About info.",
        )

    payload["name"] = about.get("name") or handle
    payload["description"] = about.get("description") or ""
    payload["country"] = about.get("country") or ""
    payload["joinedAt"] = about.get("joinedAt") or ""
    payload["links"] = about.get("links") or []


def _swap_avatar_url(payload: Dict[str, Any], row: UserChannel) -> Dict[str, Any]:
    """If the row has an archived avatar in R2, replace
    payload['avatarUrl'] with a signed R2 URL. Otherwise return as-is.

    The DB stores YouTube's URL inside data_json; on read we substitute
    our archived copy so the frontend never serves the YouTube CDN
    directly. R2 egress is free + signed URLs cache for 1 hour.
    """
    if row.avatar_r2_key:
        try:
            payload["avatarUrl"] = r2.presign_get(
                row.avatar_r2_key, expires_in=3600, subject=row.user_id
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "presign_get failed for avatar %s", row.avatar_r2_key
            )
    return payload


def _swap_thumbnail_url(
    payload: Dict[str, Any], row: UserChannelVideo
) -> Dict[str, Any]:
    """Same idea as _swap_avatar_url, but per video."""
    if row.thumbnail_r2_key:
        try:
            payload["thumbnailUrl"] = r2.presign_get(
                row.thumbnail_r2_key, expires_in=3600, subject=row.user_id
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "presign_get failed for thumbnail %s", row.thumbnail_r2_key
            )
    return payload


@router.get("/channels")
def list_channels(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    """Return all *active* subscriptions for the current user, newest-
    subscribed first. Soft-deleted (unsubscribed) subscriptions are
    excluded — those live at /channels/removed during the 30-day grace
    window before the cleanup sweep deletes their files.

    Reads from the shared-pool tables (Channel +
    UserChannelSubscription). Response shape mirrors what the YouTube
    page consumes today, assembled by archive.channel_response_payload.
    """
    from app import archive as archive_lib  # noqa: WPS433
    from app.models import Channel as _Channel  # noqa: WPS433

    rows = (
        db.query(UserChannelSubscription, _Channel)
        .join(_Channel, _Channel.id == UserChannelSubscription.channel_id)
        .filter(
            UserChannelSubscription.user_id == current.id,
            UserChannelSubscription.unsubscribed_at.is_(None),
        )
        .order_by(UserChannelSubscription.subscribed_at.desc())
        .all()
    )

    # Real archived-video count per channel — counted from THIS USER's own
    # rows. The shared-pool Video table is keyed to the channel, not the
    # user, so counting r2_key there would attribute another subscriber's
    # (or a deleted account's) archives to this user.
    archived_counts: Dict[str, int] = {}
    for legacy_video in db.query(UserChannelVideo).filter(
        UserChannelVideo.user_id == current.id
    ):
        try:
            if (json.loads(legacy_video.data_json) or {}).get(
                "status"
            ) != "archived":
                continue
        except (json.JSONDecodeError, TypeError):
            continue
        archived_counts[legacy_video.channel_id] = (
            archived_counts.get(legacy_video.channel_id, 0) + 1
        )

    # Channels whose comments we can sync: either a real web-OAuth link, OR a
    # live worker ownership (the worker now fetches comments with yt-dlp +
    # cookies, same as it does metadata and private videos). Both keyed by
    # youtube id so `channel.youtube_id in comment_capable` works.
    oauth_linked = {
        cid
        for (cid, gid) in db.query(
            UserChannel.channel_id, UserChannel.google_user_id
        ).filter(UserChannel.user_id == current.id)
        if gid
    }
    worker_owned = {
        yt_id
        for (yt_id,) in db.query(Channel.youtube_id)
        .join(ChannelOwnership, ChannelOwnership.channel_id == Channel.id)
        .filter(
            ChannelOwnership.user_id == current.id,
            ChannelOwnership.revoked_at.is_(None),
            ChannelOwnership.user_revoked_at.is_(None),
        )
    }
    # Comment sync needs no credentials for a PUBLIC video. The worker
    # runs yt-dlp with --write-comments, which is added independently of
    # the cookies file - measured: 15 real comments off a public video
    # with no cookies at all. This condition was written for the Data API
    # era, when comments genuinely did require OAuth, and it kept greying
    # the control out long after the worker made it unnecessary.
    #
    # Ownership still matters, but for coverage rather than permission:
    # only the owner's credentials can reach comments on private and
    # unlisted videos. Every tracked channel can now sync the public set.
    tracked_channels = {
        cid
        for (cid,) in db.query(UserChannel.channel_id).filter(
            UserChannel.user_id == current.id,
            UserChannel.removed_at.is_(None),
        )
    }
    comment_capable = oauth_linked | worker_owned | tracked_channels

    # The settings card's Authentication control has to be right on a cold
    # load, not only after the click that revoked it, so the caller's own
    # sticky revocation rides along with each channel.
    revoked_pks = _user_revoked_channel_pks(db, current.id)

    # How many videos each channel HAS, as far as this caller is allowed
    # to know. Same filter the video list itself uses, so the ratio on the
    # card can never disagree with the page it links to - a subscriber who
    # cannot see the owner's private videos must not be told they exist by
    # a denominator that counts them.
    known_counts: Dict[str, int] = {
        channel.youtube_id: (
            db.query(func.count(Video.id))
            .filter(Video.channel_id == channel.id)
            .filter(access.visible_video_filter(db, current.id, channel.id))
            .scalar()
            or 0
        )
        for _, channel in rows
    }

    return [
        {
            **archive_lib.channel_response_payload(
                channel,
                subscription,
                # One byte-sum feeds both stats (walrus binds before the
                # kwarg below reads it), so Storage x rate = Cost on screen.
                projected_monthly_cost_usd=archive_lib.channel_projected_monthly_cost_usd(
                    db, channel, current,
                    total_bytes=(
                        bb := archive_lib.channel_billable_bytes(
                            db, channel, current
                        )
                    ),
                ),
                billable_bytes=bb,
                archived_video_count=archived_counts.get(channel.youtube_id, 0),
                known_video_count=known_counts.get(channel.youtube_id, 0),
                comments_sync_available=channel.youtube_id in comment_capable,
            ),
            "ownershipRevoked": channel.id in revoked_pks,
            # Whether the worker has proven access to this channel's
            # private videos. Already computed above for other reasons;
            # surfaced so the channel list can be filtered by it without
            # asking per channel.
            "authenticated": channel.youtube_id in worker_owned,
        }
        for subscription, channel in rows
    ]


@router.get("/channels/removed")
def list_removed_channels(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    """Channels the user removed that are still inside the grace window —
    kept (not billed) so they can be restored by re-adding them, or wiped
    right now via POST /channels/{channel_id}/purge.

    Reads the legacy UserChannel rows: that's where removed_at and the
    archived files (avatar + videos) live. Rows already past the window are
    omitted here — the daily purge cron drops those.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=SOFT_DELETE_GRACE_DAYS)
    rows = (
        db.query(UserChannel)
        .filter(
            UserChannel.user_id == current.id,
            UserChannel.removed_at.is_not(None),
            UserChannel.removed_at >= cutoff,
        )
        .order_by(UserChannel.removed_at.desc())
        .all()
    )
    out: List[Dict[str, Any]] = []
    for row in rows:
        try:
            data = json.loads(row.data_json)
        except json.JSONDecodeError:
            data = {}
        removed_at = row.removed_at
        purge_at = (
            removed_at + timedelta(days=SOFT_DELETE_GRACE_DAYS)
            if removed_at
            else None
        )
        payload = {
            "id": row.channel_id,
            "name": data.get("name") or data.get("handle") or "Channel",
            "handle": data.get("handle") or "",
            "avatarUrl": data.get("avatarUrl") or "",
            "removedAt": removed_at.isoformat() if removed_at else None,
            "purgeAt": purge_at.isoformat() if purge_at else None,
        }
        out.append(_swap_avatar_url(payload, row))
    return out


@router.post("/channels/{channel_id}/purge", status_code=204)
def purge_removed_channel(
    channel_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Response:
    """Permanently delete one already-removed channel NOW, before the grace
    window ends. Irreversible: drops the archived files from R2 and every
    legacy DB row via the exact same path the daily purge cron uses. Only
    touches the caller's own channels, and only ones already soft-deleted.
    """
    row = db.get(UserChannel, (current.id, channel_id))
    if row is None or row.removed_at is None:
        raise HTTPException(
            status_code=404,
            detail="No removed channel found to delete.",
        )
    from scripts.purge_removed import purge_channel  # noqa: WPS433

    purge_channel(db, current.id, channel_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/connected-channels")
def list_connected_channels(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    """Channels the user's connected account owns that aren't tracked yet.

    Surfaced on the YouTube page so the user can import + sync them without
    pasting a URL. Basic users connect via their own worker app; their owned
    channels are recorded as ChannelOwnership. Excludes channels already
    tracked (active subscription).
    """
    tracked = {
        cid
        for (cid,) in db.query(UserChannelSubscription.channel_id)
        .filter(
            UserChannelSubscription.user_id == current.id,
            UserChannelSubscription.unsubscribed_at.is_(None),
        )
        .all()
    }
    owned = (
        db.query(Channel)
        .join(ChannelOwnership, ChannelOwnership.channel_id == Channel.id)
        .filter(
            ChannelOwnership.user_id == current.id,
            ChannelOwnership.revoked_at.is_(None),
        )
        .order_by(ChannelOwnership.authenticated_at.asc())
        .all()
    )
    return [
        {
            "id": c.id,
            "youtubeId": c.youtube_id,
            "handle": c.handle,
            "title": c.title,
            "thumbnailUrl": c.thumbnail_url,
        }
        for c in owned
        if c.id not in tracked
    ]


# ---- Per-channel authentication --------------------------------------------
# ChannelOwnership.channel_id is our INTERNAL Channel.id, never the YouTube
# UC id the frontend passes around, so every one of these routes resolves the
# Channel row first. The two helpers below exist so that mapping is written
# once instead of four times.


def _own_channel_ownership(
    db: Session, user_id: str, channel_pk: str
) -> Optional[ChannelOwnership]:
    """The caller's OWN ownership row for a channel, or None.

    Never widen this to "any ownership of the channel": ownership is per
    (user, channel) and multi-owner is supported for shared team channels,
    so one owner withdrawing their credential must not disturb another's.
    """
    return (
        db.query(ChannelOwnership)
        .filter(
            ChannelOwnership.user_id == user_id,
            ChannelOwnership.channel_id == channel_pk,
        )
        .one_or_none()
    )


def _user_revoked_channel_pks(db: Session, user_id: str) -> set[str]:
    """Internal Channel.ids this user has deliberately revoked. One query,
    because the channel list renders every card at once.
    """
    return {
        cid
        for (cid,) in db.query(ChannelOwnership.channel_id).filter(
            ChannelOwnership.user_id == user_id,
            ChannelOwnership.user_revoked_at.is_not(None),
        )
    }


def _user_revoked_channel_youtube_ids(db: Session, user_id: str) -> List[str]:
    """YouTube ids of the channels this user has withdrawn worker access to.

    The worker speaks YouTube ids, never our internal Channel.id, so this is
    the id-space sibling of _user_revoked_channel_pks above.

    Reads ``user_revoked_at`` and only that. ``revoked_at`` is machine
    bookkeeping that the worker's own ownership reports clear through
    archive.ensure_ownership() on every app launch, so a signal built on it
    would switch itself back off within minutes of the user acting. The
    sticky column is the human's decision and it is the only thing we are
    willing to hand the worker as an instruction to disconnect.
    """
    return [
        yt
        for (yt,) in db.query(Channel.youtube_id)
        .join(ChannelOwnership, ChannelOwnership.channel_id == Channel.id)
        .filter(
            ChannelOwnership.user_id == user_id,
            ChannelOwnership.user_revoked_at.is_not(None),
        )
        .all()
        if yt
    ]


def _authentication_payload(
    db: Session,
    user_id: str,
    own: Optional[ChannelOwnership],
) -> Dict[str, Any]:
    """The shape every authentication route returns, so the card renders
    identically whether it just acted or is coming back from a reload.
    ``revokedAt`` reports the user's own revocation instant, not the
    machine-side ``revoked_at`` - that one gets cleared by routine worker
    chatter and is not something to show a human.

    The timestamp goes out with an explicit UTC offset. We store UTC, but
    SQLite keeps no zone, so a value read back from the row is naive - and
    the card renders it through new Date(), which reads an offset-less
    string as LOCAL time. Sent naive, a revoke at 6pm Pacific comes back
    stamped the NEXT day, on the one line telling the user when they turned
    this off.
    """
    wc = db.get(WorkerYoutubeConnection, user_id)
    user_revoked_at = own.user_revoked_at if own is not None else None
    if user_revoked_at is not None and user_revoked_at.tzinfo is None:
        user_revoked_at = user_revoked_at.replace(tzinfo=timezone.utc)
    return {
        "authenticated": own is not None and own.revoked_at is None,
        "workerConnected": bool(wc and wc.connected),
        "userRevoked": user_revoked_at is not None,
        "revokedAt": user_revoked_at.isoformat() if user_revoked_at else None,
    }


@router.get("/channels/{channel_id}/authentication")
def channel_authentication_status(
    channel_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Per-channel authentication status. ``authenticated`` is True when the
    user holds a non-revoked ownership of this channel - that's what unlocks
    its sealed (private / members-only) videos for syncing. ``workerConnected``
    reports whether the user's worker app is reporting a YouTube connection
    (where ownership gets proven for Basic tier). ``userRevoked`` is the
    sticky flag set by DELETE below; the card reads it so a cold page load
    shows the withdrawal rather than whatever the worker last reported.
    """
    channel = (
        db.query(Channel).filter(Channel.youtube_id == channel_id).one_or_none()
    )
    if channel is None:
        return {
            "authenticated": False,
            "workerConnected": False,
            "userRevoked": False,
            "revokedAt": None,
        }
    return _authentication_payload(
        db, current.id, _own_channel_ownership(db, current.id, channel.id)
    )


@router.delete("/channels/{channel_id}/authentication")
def revoke_channel_authentication(
    channel_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Withdraw this user's worker authentication for one channel.

    What it does: stops NEW sealed (private / unlisted / members-only)
    videos being discovered and synced for this channel. It is purely a
    permission withdrawal.

    What it does NOT do: it deletes nothing, ever. Every video already
    archived stays archived, viewable, and downloadable - read access is not
    gated on ownership anywhere in the serving path today. Public videos are
    unaffected entirely; those follow the subscription, not ownership.

    It also does not touch the bill. We charge for what we store for as
    long as we store it, and revoking stores nothing less, so
    compute_user_byte_hours_v2 keeps metering the sealed bytes we still
    hold. Removing the channel is what stops the meter, because that is
    what soft-deletes the storage. If a user asks why revoking did not
    lower their bill, that is the answer, not a bug.

    Stickiness lives in ``user_revoked_at``. archive.py's ensure_ownership()
    clears ``revoked_at`` on every worker ownership report - which the
    desktop app sends on each launch - so a revoke written to ``revoked_at``
    alone would silently undo itself within minutes. The guard there refuses
    to clear it while ``user_revoked_at`` is set, and only POST below clears
    that.

    Deliberately on the plain current-user dependency: a user whose payment
    lapsed must still be able to withdraw a credential they gave us.
    """
    channel = (
        db.query(Channel).filter(Channel.youtube_id == channel_id).one_or_none()
    )
    if channel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Channel not found.",
        )
    own = _own_channel_ownership(db, current.id, channel.id)
    if own is None:
        # Revoking something we were never granted is a successful no-op.
        # There is nothing to withdraw and nothing to record - forging an
        # ownership row here would invent a credential that never existed.
        return _authentication_payload(db, current.id, None)
    now = datetime.now(timezone.utc)
    # Only stamp what is unset. A second call must return the ORIGINAL
    # instant, because that instant is user-visible: the settings card
    # prints it as when they turned this off. Re-stamping would move that
    # line to "just now" every time the page re-issued the DELETE, so a
    # user who revoked last month would be told they revoked today.
    if own.user_revoked_at is None:
        own.user_revoked_at = now
    if own.revoked_at is None:
        own.revoked_at = now
    db.commit()
    return _authentication_payload(db, current.id, own)


@router.post("/channels/{channel_id}/authentication")
def restore_channel_authentication(
    channel_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Re-authenticate this channel: the explicit user action that undoes
    the revoke above. Clears the sticky ``user_revoked_at`` unconditionally,
    because only the human can lift their own decision.

    ``revoked_at`` is the machine-side flag, so it only gets cleared when we
    have evidence the worker still holds the login - the same
    WorkerYoutubeConnection signal the status route reports. With that
    evidence we clear it now and report ``authenticated`` true. Without it we
    leave it set and report false: the worker's next ownership report clears
    it through archive.py's ensure_ownership(), which is free to do so again
    now that ``user_revoked_at`` is NULL. Saying "authenticated" before the
    worker has proven anything would be a promise we cannot keep.
    """
    channel = (
        db.query(Channel).filter(Channel.youtube_id == channel_id).one_or_none()
    )
    if channel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Channel not found.",
        )
    own = _own_channel_ownership(db, current.id, channel.id)
    if own is None:
        # No credential was ever given for this channel, and we cannot
        # manufacture one - ownership is proven by the worker reporting it is
        # signed in as the channel. The response says authenticated false,
        # which is the truth, and the card points at the worker app.
        return _authentication_payload(db, current.id, None)
    own.user_revoked_at = None
    wc = db.get(WorkerYoutubeConnection, current.id)
    if wc and wc.connected and own.revoked_at is not None:
        own.revoked_at = None
    db.commit()
    return _authentication_payload(db, current.id, own)


# ---- Soft-delete grace window ----------------------------------------------
# When a user removes a channel, we mark it with removed_at instead of
# hard-deleting. The daily purge cron walks rows where removed_at is
# older than this and actually deletes them (DB rows + R2 keys). 30
# days matches Google Drive Trash, Dropbox restore, etc.
#
# There is no UI for restoring a removed channel - that would let
# users skip the OAuth re-add flow, which is the friction we want.
# Re-importing the channel via Settings (re-OAuth + Import) clears
# the removed_at flag automatically, so any data still inside the
# grace window is reused. After 30 days the purge cron drops it.
SOFT_DELETE_GRACE_DAYS = 30


@router.post("/channels", status_code=status.HTTP_201_CREATED)
def add_channel(
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
    current: User = Depends(get_paid_user),
) -> Dict[str, Any]:
    """Add a channel for the current user. Rejects duplicates.

    If saveChannelAvatar is on, the real profile picture URL is fetched
    from YouTube. If YouTube is unreachable, the channel is NOT added.

    Requires an active payment method — adding a channel triggers
    discovery + per-video metadata fetches that cost us money.
    """
    channel_id = str(payload.get("id") or "").strip()
    if not channel_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Channel id is required.",
        )
    existing = db.get(UserChannel, (current.id, channel_id))
    if existing is not None and existing.removed_at is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Channel already added.",
        )

    _resolve_avatar(payload, channel_id)  # may raise 502
    _resolve_about(payload, channel_id)  # may raise 502
    _resolve_stats(payload, channel_id, existing=None)  # may raise 502

    if existing is not None:
        # Was soft-deleted - restore it instead of inserting a duplicate.
        # The archived data inside the 30-day grace window gets reused.
        existing.data_json = json.dumps(payload)
        existing.removed_at = None
        # Resume billing for this channel's storage objects.
        storage_ledger.propagate_channel_restore(db, current.id, channel_id)
        row = existing
    else:
        row = UserChannel(
            user_id=current.id,
            channel_id=channel_id,
            google_user_id=_first_google_user_id(db, current.id),
            data_json=json.dumps(payload),
        )
        db.add(row)
    _archive_channel_avatar(db, row, payload)
    # Always discover videos on add — discovery is no longer toggle-gated.
    # The downloadNewVideos toggle now controls future file syncing only.
    _full_sync_videos(db, current.id, channel_id)  # may raise 502
    # Strip any per-video metadata fields the user opted out of in defaults.
    _apply_video_metadata_toggles(
        db, current.id, channel_id, payload.get("settings") or {}, prev_settings=None
    )
    db.commit()
    return payload


@router.post("/channels/track", status_code=status.HTTP_201_CREATED)
def track_channel_by_url(
    payload: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    current: User = Depends(get_paid_user),
) -> Dict[str, Any]:
    """Start archiving any YouTube channel from a URL / handle / UC id.

    Distinct from POST /channels (which is the owner-import flow from
    OAuth). This is the public-observation flow: the user pastes a
    channel URL on the YouTube page and we start tracking it. They
    only get access to publicly-visible content (private + members-
    only videos remain hidden); if the channel owner separately
    authenticates with us later, that unlocks their tier of access
    without affecting other subscribers.

    Body:  { "input": "https://youtube.com/@MrBeast" }
                          or "@MrBeast"
                          or "UCxxxxxxxxxxxxxxxxxxxxxx"
                          or "youtube.com/channel/UCxxx..."

    Returns the legacy-shaped channel payload so the frontend's
    existing /channels list rendering keeps working as-is.
    """
    from app import archive as archive_lib

    raw = str(payload.get("input") or "").strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An input URL or handle is required.",
        )

    channel_id = resolve_channel_id(raw)
    if not channel_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Couldn't resolve that input to a YouTube channel. "
                "Try the channel's URL (youtube.com/@handle or "
                "youtube.com/channel/UC…)."
            ),
        )

    # Reject duplicates the same way POST /channels does. Reactivating
    # a soft-deleted UserChannel mirrors the owner-import flow.
    existing = db.get(UserChannel, (current.id, channel_id))
    if existing is not None and existing.removed_at is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You're already tracking that channel.",
        )

    # Fetch the minimum metadata we need to render a usable card on
    # the YouTube page. fetch_channel_about gives us the canonical
    # title + description.
    about = fetch_channel_about(channel_id)
    if about is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Couldn't reach YouTube to fetch channel info.",
        )

    avatar_url = fetch_channel_avatar_url(channel_id) or ""
    stats = fetch_channel_stats(channel_id) or {
        "subscriberCount": 0,
        "videoCount": 0,
        "totalViews": 0,
    }

    # Construct the same payload shape POST /channels stores. Match
    # the frontend Channel type (name, not title; addedAt set so the
    # list orders correctly; etc.). Defaults leave most settings off;
    # user can flip them in channel detail.
    # The channel's real @handle.
    #
    # YouTube's canonical form wins over what the user typed, because the
    # Channel row is SHARED between every subscriber: handles are
    # case-insensitive as addresses, so one person pasting "@AFRFX" and
    # another "@afrfx" would otherwise leave the displayed handle decided
    # by whoever happened to add the channel first. What they typed is
    # the fallback for when YouTube is unreachable, and for the /channel/
    # URL form there is nothing to fall back to.
    #
    # Either beats what this used to do, which was "@" + the display
    # NAME - a different string that matches often enough to look right.
    # "AFRFX" happens to give "@AFRFX"; "Afraaz 🗿" gives "@Afraaz 🗿",
    # an address that does not exist.
    handle = fetch_channel_handle(channel_id) or _handle_from_input(raw) or ""

    now_iso = datetime.now(timezone.utc).isoformat()
    legacy_payload: Dict[str, Any] = {
        "id": channel_id,
        "handle": handle or channel_id,
        "name": about.get("name") or channel_id,
        "description": about.get("description") or "",
        "avatarUrl": avatar_url,
        "subscriberCount": stats["subscriberCount"],
        "videoCount": stats["videoCount"],
        "totalViews": stats["totalViews"],
        "country": about.get("country") or "",
        "joinedAt": about.get("joinedAt") or "",
        "links": about.get("links") or [],
        "addedAt": now_iso,
        "lastSyncedAt": now_iso,
        "terminatedAt": None,
        "youtubeStatus": "available",
        # The user's configured New-channel-defaults over the app baseline.
        # This used to hardcode every key except "active", which made the
        # defaults panel decorative.
        "settings": _new_channel_settings(db, current.id),
    }

    if existing is not None:
        # Soft-deleted row — restore the same way POST /channels does.
        existing.data_json = json.dumps(legacy_payload)
        existing.removed_at = None
        storage_ledger.propagate_channel_restore(db, current.id, channel_id)
        row = existing
    else:
        row = UserChannel(
            user_id=current.id,
            channel_id=channel_id,
            google_user_id=None,  # public-observation flow, no OAuth
            data_json=json.dumps(legacy_payload),
        )
        db.add(row)

    _archive_channel_avatar(db, row, legacy_payload)
    # Deliberately NOT calling _full_sync_videos here. It runs yt-dlp on the
    # Hetzner box, which YouTube bot-checks roughly nine times in ten, and a
    # None return raises 502 out of the helper - so a scrape that YouTube
    # happened to refuse failed the entire channel-add. It was also capped at
    # max_pages * 30 = 900 videos, so even on a good day it could not see the
    # back catalogue of the channels this product is for.
    #
    # Enumeration belongs on the user's machine, where there is no bot check
    # and no cap: measured 7,264 videos off a real uploads playlist in 27s
    # with no cookies at all. The worker reports them to
    # POST /worker/discovered-videos.
    _apply_video_metadata_toggles(
        db,
        current.id,
        channel_id,
        legacy_payload.get("settings") or {},
        prev_settings=None,
    )

    # Mirror the shared-pool tables. Channel + Subscription get
    # created here; Video rows land via record_synced_video as the
    # worker pool finishes individual sync jobs. No ChannelOwnership
    # because this is the public-observation flow.
    new_channel = archive_lib.ensure_channel(
        db,
        channel_id,
        title=legacy_payload["name"],
        handle=handle or None,
        thumbnail_url=avatar_url or None,
    )
    # ensure_channel only fills optional fields when it CREATES the row,
    # which is right for titles someone may have corrected but wrong for
    # a handle that is simply missing. Channels tracked before this flow
    # learned to capture one have an empty column and would keep it
    # forever, rendering as a raw UC id on their own page.
    if handle and not new_channel.handle:
        new_channel.handle = handle
    # Populate the YouTube-side rich info blob so the read routes
    # render the same fields the legacy data_json holds.
    if not new_channel.metadata_json:
        channel_meta = {
            k: v
            for k, v in legacy_payload.items()
            if k
            not in ("id", "name", "handle", "avatarUrl", "settings", "addedAt", "lastSyncedAt")
        }
        new_channel.metadata_json = json.dumps(channel_meta)
    new_sub = archive_lib.ensure_subscription(
        db, current.id, new_channel.id
    )
    # Per-user channel preferences live on the subscription row.
    if not new_sub.settings_json:
        new_sub.settings_json = json.dumps(legacy_payload.get("settings") or {})
    new_sub.last_synced_at = datetime.now(timezone.utc)

    # Subscribe to YouTube's PubSubHubbub feed so we hear about new
    # uploads in seconds rather than waiting for the next sync poll.
    # Best-effort: a failed subscribe doesn't break the track flow,
    # the renewal cron will retry the next day. Skip entirely on
    # BASE_URL-less dev boxes (the hub can't call back to localhost).
    if new_channel.pubsub_lease_expires_at is None:
        try:
            if pubsub.subscribe_channel(channel_id):
                new_channel.pubsub_last_renewed_at = datetime.now(
                    timezone.utc
                )
                # Verification handshake is async; the hub-side lease
                # only starts after our callback echoes back. Stamp a
                # conservative 10-day expiry now so the renewal cron
                # has something to compare against.
                new_channel.pubsub_lease_expires_at = datetime.now(
                    timezone.utc
                ) + timedelta(days=10)
        except RuntimeError as exc:
            # BASE_URL missing - just log + continue. Useful in dev.
            log.info("skipping pubsub subscribe: %s", exc)
        except Exception:
            log.exception("pubsub subscribe failed for %s", channel_id)

    db.commit()
    return legacy_payload


@router.get("/channels/{channel_id}")
def get_channel(
    channel_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return a single channel for the current user. channel_id is the
    YouTube UC id; we look up our internal Channel row by youtube_id,
    then the user's subscription to it."""
    from app import archive as archive_lib  # noqa: WPS433
    from app.models import Channel as _Channel  # noqa: WPS433

    channel = (
        db.query(_Channel).filter(_Channel.youtube_id == channel_id).one_or_none()
    )
    if channel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Channel not found.",
        )
    sub = (
        db.query(UserChannelSubscription)
        .filter(
            UserChannelSubscription.user_id == current.id,
            UserChannelSubscription.channel_id == channel.id,
            UserChannelSubscription.unsubscribed_at.is_(None),
        )
        .one_or_none()
    )
    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Channel not found.",
        )
    legacy_row = db.get(UserChannel, (current.id, channel_id))
    billable = archive_lib.channel_billable_bytes(db, channel, current)
    payload = archive_lib.channel_response_payload(
        channel,
        sub,
        projected_monthly_cost_usd=archive_lib.channel_projected_monthly_cost_usd(
            db, channel, current, total_bytes=billable
        ),
        billable_bytes=billable,
        known_video_count=(
            db.query(func.count(Video.id))
            .filter(Video.channel_id == channel.id)
            .filter(access.visible_video_filter(db, current.id, channel.id))
            .scalar()
            or 0
        ),
        # Tracking is enough - same rule as list_channels. This was a
        # SECOND copy of that expression and it did not get updated with
        # the first, which would have been worse than useless: the channel
        # detail page reads THIS route, so the fix would have appeared to
        # do nothing exactly where the user was looking.
        comments_sync_available=True,
    )
    # Same reason as the list route: the Authentication card is rendered
    # from the channel payload on a cold load, before it fetches its own
    # status, so the caller's sticky revocation travels with the channel.
    own = _own_channel_ownership(db, current.id, channel.id)
    payload["ownershipRevoked"] = (
        own is not None and own.user_revoked_at is not None
    )
    return payload


@router.put("/channels/{channel_id}")
def update_channel(
    channel_id: str,
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Replace the stored channel data for the given channel.

    If saveChannelAvatar just turned on (or was on but the stored URL is a
    placeholder), the real avatar URL is fetched from YouTube. Returns 502
    if YouTube is unreachable — caller should keep their current state.

    Returns the updated channel so the caller can pick up any backend-side
    mutations (e.g. the freshly fetched avatar URL).
    """
    row = db.get(UserChannel, (current.id, channel_id))
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Channel not found.",
        )
    existing: Optional[Dict[str, Any]] = None
    try:
        existing = json.loads(row.data_json)
    except json.JSONDecodeError:
        existing = None
    _resolve_avatar(payload, channel_id, existing=existing)  # may raise 502
    _resolve_about(payload, channel_id)  # may raise 502
    _resolve_stats(payload, channel_id, existing=existing)  # may raise 502
    # NOTE: PUT does not run video discovery (that's the manual Sync button's
    # job). It does react to per-video metadata toggle transitions —
    # off→on triggers a per-video fetch, on→off strips the field.
    _apply_video_metadata_toggles(
        db,
        current.id,
        channel_id,
        payload.get("settings") or {},
        prev_settings=(existing or {}).get("settings"),
    )
    # Last gate before the avatar url touches the DB. _resolve_avatar already
    # blanked a round-tripped presigned url and refetched when the toggle is
    # on; with the toggle off there is nothing to refetch, so hold onto the
    # last real CDN url we stored rather than writing an empty string over it.
    # Only a url that survives _is_own_storage_url is allowed to persist -
    # a signature would expire within the hour and take the CDN url with it.
    stored_avatar = (payload.get("avatarUrl") or "").strip()
    if not stored_avatar:
        prior = (existing or {}).get("avatarUrl")
        stored_avatar = "" if _is_own_storage_url(prior) else (prior or "")
    payload["avatarUrl"] = stored_avatar

    row.data_json = json.dumps(payload)
    _archive_channel_avatar(db, row, payload)

    # Mirror the write to the shared-pool Channel + Subscription so
    # the new-model read routes serve the same fresh values.
    from app import archive as archive_lib  # noqa: WPS433
    from app.models import Channel as _Channel  # noqa: WPS433

    new_channel = (
        db.query(_Channel)
        .filter(_Channel.youtube_id == channel_id)
        .one_or_none()
    )
    if new_channel is not None:
        if payload.get("name"):
            new_channel.title = payload["name"]
        if payload.get("handle"):
            new_channel.handle = payload["handle"]
        # thumbnail_url is the CDN fallback archive.py reaches for when the
        # archived object is gone, so it has to stay a YouTube url. Writing a
        # presigned one here is what killed the fallback the first time.
        if stored_avatar:
            new_channel.thumbnail_url = stored_avatar
        channel_meta = {
            k: v
            for k, v in payload.items()
            if k not in ("id", "name", "handle", "avatarUrl", "settings", "addedAt", "lastSyncedAt")
        }
        new_channel.metadata_json = json.dumps(channel_meta)

        new_sub = (
            db.query(UserChannelSubscription)
            .filter(
                UserChannelSubscription.user_id == current.id,
                UserChannelSubscription.channel_id == new_channel.id,
            )
            .one_or_none()
        )
        if new_sub is not None:
            new_sub.settings_json = json.dumps(payload.get("settings") or {})

    db.commit()
    return payload


@router.post("/channels/{channel_id}/sync-metadata")
def sync_channel_metadata(
    channel_id: str,
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
    current: User = Depends(get_paid_user),
) -> Dict[str, int]:
    """Refresh per-video metadata for every video already discovered
    on this channel via the YouTube Data API (OAuth).

    Body: {fields: {saveThumbnail, saveViewCount, saveDescription, saveTags}}

    Owned-channel flow: uses the channel's google_user_id to load
    OAuth creds and calls youtube.videos().list batched 50-at-a-time.
    This sees private + unlisted videos that public scraping can't,
    and gets fresh thumbnail bytes (we always re-archive thumbnails
    when the toggle is on, since YouTube CDN URLs are stable per
    video but the bytes behind them change when the channel owner
    edits the thumbnail).

    Public scraping (youtube_scrape.fetch_video_metadata) is reserved
    for future third-party / public-channel support and is not used
    on this path.

    Doesn't run video discovery - assumes the catalog is current.
    Use /sync first if needed.
    """
    row = db.get(UserChannel, (current.id, channel_id))
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Channel not found.",
        )
    fields = payload.get("fields") or {}
    want = {
        "thumbnail": bool(fields.get("saveThumbnail")),
        "viewCount": bool(fields.get("saveViewCount")),
        "description": bool(fields.get("saveDescription")),
        "tags": bool(fields.get("saveTags")),
        # comments / captions: deferred features (silently no-op)
    }

    # Same story as the discovery route: a worker-only user has no
    # credentials and cannot get any, so this 400 was permanent for them -
    # "Metadata refresh returned 400." every time they pressed Sync.
    #
    # Metadata on a public video is public. The worker refreshes it through
    # the metadata-jobs path, so an absent credential means "not this
    # route's job", not "you did something wrong".
    creds = _load_user_credentials(db, current.id, row.google_user_id)
    if creds is None:
        return {"checked": 0, "updated": 0, "skipped": 0}

    rows = (
        db.query(UserChannelVideo)
        .filter(
            UserChannelVideo.user_id == current.id,
            UserChannelVideo.channel_id == channel_id,
        )
        .all()
    )
    if not rows:
        return {"refreshed": 0, "skipped": 0}

    video_ids = [r.video_id for r in rows]
    try:
        items = google_oauth.fetch_video_details(creds, video_ids)
    except Exception:
        log.exception(
            "sync-metadata: fetch_video_details failed for user %s channel %s",
            current.id,
            channel_id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Couldn't reach YouTube to refresh metadata.",
        )

    by_id = {item.get("id"): item for item in items if item.get("id")}

    refreshed = 0
    skipped = 0
    thumbnail_jobs: List[Tuple[UserChannelVideo, str]] = []

    for r in rows:
        item = by_id.get(r.video_id)
        if item is None:
            # YouTube Data API didn't return this video. Could be
            # deleted, region-blocked, or temporarily missing. Leave
            # the row as-is and count it as skipped.
            skipped += 1
            continue
        try:
            data = json.loads(r.data_json)
        except json.JSONDecodeError:
            continue
        snippet = item.get("snippet") or {}
        content = item.get("contentDetails") or {}
        stats = item.get("statistics") or {}
        status_obj = item.get("status") or {}
        duration_sec = _parse_iso_duration(content.get("duration") or "")
        new_thumb_url = _pick_thumbnail(snippet)

        if want["thumbnail"] and new_thumb_url:
            data["thumbnailUrl"] = new_thumb_url
            # Always queue for R2 re-archive when thumbnail toggle is
            # on - YouTube CDN URLs are stable per video so a URL diff
            # check would false-negative whenever the channel owner
            # edits the thumbnail. The parallel uploader overwrites
            # the key in place.
            thumbnail_jobs.append((r, new_thumb_url))
        if want["viewCount"]:
            data["viewCount"] = int(stats.get("viewCount") or 0)
        if want["description"]:
            data["description"] = snippet.get("description") or ""
        if want["tags"]:
            data["tags"] = snippet.get("tags") or []
        # Always upgrade rough fields when we touch a video - exact
        # upload date / duration / privacy are strictly better than
        # whatever the row had before.
        if snippet.get("publishedAt"):
            data["uploadDate"] = snippet["publishedAt"]
        if duration_sec:
            data["durationSec"] = duration_sec
        privacy = _privacy_from_status(status_obj.get("privacyStatus"))
        if privacy:
            data["privacy"] = privacy
        r.data_json = json.dumps(data)
        refreshed += 1

    # Re-archive any updated thumbnails to R2 in parallel before
    # commit. Failures are logged + skipped (the URL still works as
    # a fallback at read time).
    _archive_thumbnails_parallel(db, thumbnail_jobs)

    db.commit()
    return {"refreshed": refreshed, "skipped": skipped}


def _oauth_full_sync_videos(
    db: Session,
    user_id: str,
    channel_id: str,
) -> Dict[str, int]:
    """OAuth-authenticated catalog reconciler.

    Uses the channel owner's stored Google credentials to call the
    YouTube Data API directly. This replaces the older public-scrape
    path (kept around for hypothetical future third-party channels)
    because:
      - Hetzner's data-center IPs get bot-blocked by YouTube on the
        InnerTube/HTML endpoints the public scrape uses.
      - The Data API returns every video the channel owner has access
        to, including private + unlisted, which scraping can't see.

    Reconciliation:
      - Video in API response & not in DB     -> insert as 'discovered'
      - Video in API response & in DB         -> update title/views/etc;
                                                  if it was previously
                                                  marked deleted, clear
                                                  that mark.
      - Video in DB & NOT in API response     -> one removal strike; the
        SECOND consecutive strike marks deleted_on_youtube and releases the
        notification (we own this account, so the API would have returned it
        if it still existed in any form - but "the API returned it" and "the
        API was healthy" are different claims, hence the debounce and the
        empty-response guard below).
    """
    user_channel = db.get(UserChannel, (user_id, channel_id))
    if user_channel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Channel not found.",
        )
    # No Google account is NOT an error here any more.
    #
    # track_channel_by_url writes google_user_id=None by design (the
    # public-observation flow), and Basic tier cannot connect OAuth at all
    # - the start endpoint refuses it. So this 400 fired for every channel
    # added by the current product flow, for the entire desktop-worker user
    # base, permanently. It surfaced as "Discovery returned 400." under the
    # Sync panel's Videos checkbox: the button looked broken because for
    # those users it was.
    #
    # Catalogue enumeration does not need credentials - the worker does it
    # from the user's own machine, 7,264 videos in 27s with none. So the
    # OAuth path stays for users who have it, and everyone else gets an
    # honest empty result rather than a failure.
    if not user_channel.google_user_id:
        return {"discovered": 0, "updated": 0, "removed": 0}
    creds = _load_user_credentials(db, user_id, user_channel.google_user_id)
    if creds is None:
        return {"discovered": 0, "updated": 0, "removed": 0}

    # Step 1: get the uploads playlist ID via channels.list.
    try:
        from googleapiclient.discovery import build
        yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
        channel_resp = (
            yt.channels().list(id=channel_id, part="contentDetails").execute()
        )
    except Exception:
        log.exception("sync: channels.list failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Couldn't reach YouTube.",
        )
    items = channel_resp.get("items") or []
    if not items:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Channel not found via API.",
        )
    uploads_playlist = (
        (items[0].get("contentDetails") or {})
        .get("relatedPlaylists", {})
        .get("uploads")
    )

    # Step 2: fetch every video the channel owner has on this channel.
    try:
        video_ids = (
            google_oauth.fetch_all_video_ids(creds, uploads_playlist)
            if uploads_playlist
            else []
        )
        fresh = (
            google_oauth.fetch_video_details(creds, video_ids) if video_ids else []
        )
    except Exception:
        log.exception("sync: fetch_video_details failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Couldn't fetch videos from YouTube.",
        )
    fresh_by_id: Dict[str, Dict[str, Any]] = {
        v.get("id"): v for v in fresh if v.get("id")
    }

    # Step 3: reconcile against existing rows.
    existing_rows = (
        db.query(UserChannelVideo)
        .filter(
            UserChannelVideo.user_id == user_id,
            UserChannelVideo.channel_id == channel_id,
        )
        .all()
    )
    existing_by_id: Dict[str, UserChannelVideo] = {
        r.video_id: r for r in existing_rows
    }

    # Empty-response guard. A zero-item fetch and "the owner deleted every
    # video" are indistinguishable from here, and the two outcomes are wildly
    # asymmetric: one is a quota error or a transient blank page, the other
    # would mark a user's whole archive gone and email them about it. So when
    # the fetch comes back empty while we still hold rows, we call the probe
    # failed and skip step 4 entirely. A genuinely empty channel we also have
    # no rows for falls through normally - there is nothing to mis-mark.
    probe_failed = not fresh_by_id and bool(existing_by_id)
    if probe_failed:
        log.warning(
            "sync: OAuth fetch returned 0 videos for %s/%s while %d rows "
            "exist - treating as a failed probe, marking nothing deleted",
            user_id, channel_id, len(existing_by_id),
        )

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    added = 0
    updated = 0
    deletedified = 0
    seen: set[str] = set()

    for video_id, v in fresh_by_id.items():
        if video_id in seen:
            continue
        seen.add(video_id)

        v_snippet = v.get("snippet") or {}
        v_content = v.get("contentDetails") or {}
        v_stats = v.get("statistics") or {}
        v_status = v.get("status") or {}
        duration_sec = _parse_iso_duration(v_content.get("duration") or "")

        existing_row = existing_by_id.get(video_id)
        if existing_row is not None:
            try:
                existing_data = json.loads(existing_row.data_json)
            except json.JSONDecodeError:
                existing_data = {}
        else:
            existing_data = {}

        # If the row was previously marked deleted but the video is
        # back in the API, restore it to whichever state its archive
        # file dictates (archived if we have a local file, otherwise
        # back to discovered).
        prev_status = existing_data.get("status")
        if prev_status == "deleted_on_youtube":
            payload_status = (
                "archived" if existing_data.get("localPath") else "discovered"
            )
        else:
            payload_status = prev_status or "discovered"

        payload: Dict[str, Any] = {
            "id": video_id,
            "channelId": channel_id,
            "title": v_snippet.get("title") or "",
            "description": v_snippet.get("description") or "",
            "uploadDate": v_snippet.get("publishedAt") or "",
            "durationSec": duration_sec,
            "thumbnailUrl": _pick_thumbnail(v_snippet),
            "status": payload_status,
            # A missing privacyStatus is a hole in the response, not a claim
            # that the video is public. Keep what the row already said, and
            # start an unclassifiable new row at the most restrictive tier -
            # the same default worker discovery uses, because over-exposing a
            # private video is the one mistake here we cannot take back.
            "privacy": (
                _privacy_from_status(v_status.get("privacyStatus"))
                or existing_data.get("privacy")
                or "private"
            ),
            "type": _video_type(v, duration_sec),
            "viewCount": int(v_stats.get("viewCount") or 0),
            "tags": v_snippet.get("tags") or [],
            "commentCount": int(v_stats.get("commentCount") or 0),
            "comments": existing_data.get("comments") or [],
            "captionLanguages": existing_data.get("captionLanguages") or [],
            "videoFormat": existing_data.get("videoFormat"),
            "videoResolution": existing_data.get("videoResolution"),
            "videoBitrateKbps": existing_data.get("videoBitrateKbps"),
            "localPath": existing_data.get("localPath"),
            "fileSizeBytes": existing_data.get("fileSizeBytes"),
            "firstSeenAt": existing_data.get("firstSeenAt") or now_iso,
            "archivedAt": existing_data.get("archivedAt"),
            "lastYoutubeCheckAt": now_iso,
        }
        # Video came back in the API response, so clear the deletion mark and
        # any absence strikes it had banked.
        clear_removal_marks(payload)

        if existing_row is None:
            db.add(
                UserChannelVideo(
                    user_id=user_id,
                    channel_id=channel_id,
                    video_id=video_id,
                    data_json=json.dumps(payload),
                )
            )
            added += 1
        else:
            existing_row.data_json = json.dumps(payload)
            updated += 1

    # Step 4: anything we knew about that's missing from the API is a removal
    # signal. Two consecutive signals confirm it; the first only banks a
    # strike. Nothing here ever deletes the archive - we only change status.
    # Skipped wholesale when the probe itself failed (see the guard above):
    # a failed probe is not evidence of anything.
    if not probe_failed:
        for video_id, row in existing_by_id.items():
            if video_id in seen:
                continue
            try:
                data = json.loads(row.data_json)
            except json.JSONDecodeError:
                # Unreadable blob - don't rewrite a row we can't parse, and
                # certainly don't declare its video gone.
                continue
            if note_video_missing(data, now=now):
                deletedified += 1
            row.data_json = json.dumps(data)

    db.commit()

    _notify_videos_deleted(db, user_id, channel_id, deletedified)

    return {"added": added, "updated": updated, "deletedified": deletedified}


@router.post("/channels/{channel_id}/sync")
def sync_channel(
    channel_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(get_paid_user),
) -> Dict[str, int]:
    """Manually re-sync a channel's video catalog from YouTube.

    OAuth-authenticated path via the channel owner's stored Google
    credentials. See _oauth_full_sync_videos for reconciliation
    semantics. Returns counts summary.
    """
    return _oauth_full_sync_videos(db, current.id, channel_id)


# Opaque cursor for list_channel_videos pagination. The frontend treats
# it as a black box; the server encodes the (uploadDate, video_id) of the
# last item it returned. We base64+json so the wire format stays stable
# even if we add fields to the cursor payload later (no client breakage).
#
# Tiebreaker: video_id ASC inside same uploadDate so the sort is stable
# even when YouTube reports two uploads with the exact same timestamp
# (common for bulk-imported channels). Without the tiebreaker a row
# could appear twice or get skipped across page boundaries.
def _encode_videos_cursor(upload_date: str, video_id: str) -> str:
    raw = json.dumps(
        {"uploadDate": upload_date, "video_id": video_id},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_videos_cursor(cursor: str) -> Optional[Tuple[str, str]]:
    """Returns (uploadDate, video_id) or None if the cursor is malformed.

    We deliberately swallow decode errors and treat them as 'start from
    the beginning' rather than 400ing - clients that hang onto a stale
    cursor across a backend redeploy shouldn't see a hard error.
    """
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + padding)
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    upload = payload.get("uploadDate")
    vid = payload.get("video_id")
    if not isinstance(upload, str) or not isinstance(vid, str):
        return None
    return upload, vid


@router.get("/channels/{channel_id}/videos")
def list_channel_videos(
    channel_id: str,
    cursor: Optional[str] = Query(
        None,
        description="Opaque pagination cursor returned by a previous page.",
    ),
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return a page of discovered videos for a channel, newest-first.

    Cursor pagination keeps the response bounded: a channel with 5k
    videos was previously a ~10 MB JSON blob; now the client pulls
    pages of up to ``limit`` rows and stops when ``nextCursor`` is null.
    Sort key is (uploadDate DESC, video_id ASC); the tiebreaker keeps
    the page boundary stable when two uploads share a timestamp.

    NOTE: thumbnailUrl is intentionally left as whatever data_json
    contains (the original YouTube CDN URL) - we DON'T mint a presigned
    R2 URL per video in this hot path. That was costing one R2 Class B
    op per video per page-load. Frontend should bulk-fetch presigned
    URLs for visible rows via /channels/{channel_id}/thumbnail-urls.
    """
    # Resolve the channel + verify the user is subscribed before
    # exposing any videos. Returns an empty page (not 404) for
    # unknown channels so the YouTube page handles the "channel was
    # deleted while you had it open" case without an error toast.
    from app import archive as archive_lib  # noqa: WPS433
    from app.models import Channel as _Channel  # noqa: WPS433

    channel = (
        db.query(_Channel).filter(_Channel.youtube_id == channel_id).one_or_none()
    )
    if channel is None:
        return {"items": [], "nextCursor": None}
    sub = (
        db.query(UserChannelSubscription)
        .filter(
            UserChannelSubscription.user_id == current.id,
            UserChannelSubscription.channel_id == channel.id,
            UserChannelSubscription.unsubscribed_at.is_(None),
        )
        .one_or_none()
    )
    if sub is None:
        return {"items": [], "nextCursor": None}

    # We load all matching rows then sort + paginate in Python. The
    # alternative - pushing the sort into SQL via published_at - works
    # natively now but the in-memory cost (a few MB + an O(n log n)
    # sort) is still negligible next to the wire-size win pagination
    # delivers, so keeping the existing code path for simplicity.
    # Access filter, not just a channel scope. Discovery is shared across
    # every subscriber, so without this a stranger who tracks the same
    # channel sees the OWNER's private video titles - the pool holds one
    # Video row per real video, and this endpoint used to return all of
    # them to anyone with a subscription. access.py existed for exactly
    # this rule and had no callers; now it has one, in SQL, where a new
    # row cannot slip past it.
    videos = (
        db.query(Video)
        .filter(Video.channel_id == channel.id)
        .filter(access.visible_video_filter(db, current.id, channel.id))
        .all()
    )

    # Discovery is channel-level (shared), but ARCHIVED state is per-user:
    # Video.r2_key/bytes_stored describe whichever single subscriber
    # archived the file, so trusting them would report another user's - or
    # a deleted account's - archive as this user's. Overlay the caller's own
    # rows; anything they haven't archived reads as merely discovered.
    own: Dict[str, Dict[str, Any]] = {}
    for r in db.query(UserChannelVideo).filter(
        UserChannelVideo.user_id == current.id,
        UserChannelVideo.channel_id == channel_id,
    ):
        try:
            own[r.video_id] = json.loads(r.data_json) or {}
        except (json.JSONDecodeError, TypeError):
            own[r.video_id] = {}

    decoded: List[Tuple[str, str, Dict[str, Any]]] = []
    for v in videos:
        payload = archive_lib.video_response_payload(v)
        mine = own.get(v.youtube_id)
        if mine is None:
            payload["status"] = "discovered"
            payload["fileSizeBytes"] = 0
            payload["archivedAt"] = None
            payload["localPath"] = None
        else:
            payload["status"] = mine.get("status") or "discovered"
            payload["fileSizeBytes"] = mine.get("fileSizeBytes") or 0
            payload["archivedAt"] = mine.get("archivedAt")
            # The caller's OWN storage key, never the shared row's - the
            # frontend reads localPath as "do I have this file".
            payload["localPath"] = mine.get("localPath")
            # Prefer the REAL YouTube upload date. Video.published_at falls
            # back to "now" when a video is discovered without one (true for
            # owner-private videos found via the uploads playlist), which
            # makes every such row share a discovery timestamp and renders
            # any date sort meaningless.
            real_upload = mine.get("uploadDate")
            if real_upload:
                payload["uploadDate"] = real_upload
        upload = payload.get("uploadDate") or ""
        decoded.append((upload, v.youtube_id, payload))

    # Sort by uploadDate DESC, then video_id ASC as a stable tiebreaker.
    # Empty uploadDate strings sort to the bottom (after all real dates)
    # since lexicographic compare of "" against any ISO date is "less
    # than", and we're going descending.
    #
    # We can't do this in one sort with reverse=True because that would
    # also reverse the video_id direction. Python's sort is stable, so
    # two passes (least-significant first) gets us a true mixed-order
    # sort: video_id ASC innermost, then uploadDate DESC outermost.
    decoded.sort(key=lambda t: t[1])  # video_id ASC
    decoded.sort(key=lambda t: t[0] or "", reverse=True)  # uploadDate DESC

    # Apply cursor: skip everything that comes before-or-equal-to
    # (cursor_upload, cursor_vid) under our sort order. "Strictly
    # after" so the cursor item itself isn't repeated as the first
    # item of the next page.
    #
    # We do a value-comparison skip rather than an "exact match then
    # +1" lookup so that a row deleted between page loads doesn't
    # cause us to restart from the beginning (which would duplicate
    # everything the client already has). Under the sort order
    # uploadDate DESC + video_id ASC, an item (u, v) comes BEFORE the
    # cursor (cu, cv) iff (u > cu) or (u == cu and v < cv).
    start = 0
    if cursor:
        decoded_cursor = _decode_videos_cursor(cursor)
        if decoded_cursor:
            c_upload, c_vid = decoded_cursor
            for i, (upload, vid, _) in enumerate(decoded):
                # Skip while the item is at-or-before the cursor under
                # our sort order. The first item that's strictly AFTER
                # the cursor is where we start the next page.
                before_or_equal = (upload or "") > c_upload or (
                    (upload or "") == c_upload and vid <= c_vid
                )
                if not before_or_equal:
                    start = i
                    break
            else:
                # Every row is at-or-before the cursor (i.e. we've
                # reached the end). Empty page.
                start = len(decoded)

    page = decoded[start : start + limit]
    items = [payload for (_, _, payload) in page]

    next_cursor: Optional[str] = None
    if start + limit < len(decoded) and page:
        last_upload, last_vid, _ = page[-1]
        next_cursor = _encode_videos_cursor(last_upload, last_vid)

    return {"items": items, "nextCursor": next_cursor}


@router.post("/channels/{channel_id}/thumbnail-urls")
def bulk_thumbnail_urls(
    channel_id: str,
    body: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Dict[str, str]]:
    """Bulk-mint presigned R2 URLs for a batch of video thumbnails.

    Companion endpoint to GET /channels/{channel_id}/videos. The list
    endpoint deliberately does NOT presign thumbnails (would be one R2
    Class B op per video, multiplied across thousands of rows); the
    frontend instead fetches a page of metadata, then calls THIS
    endpoint once per page to get the URLs for what it's about to
    actually render.

    Body shape: {"video_ids": ["abc", "def", ...]}
    Returns:    {"urls": {"abc": "https://...", "def": "https://..."}}

    Rows without an archived thumbnail (thumbnail_r2_key is null - e.g.
    the channel hasn't synced yet, or the thumbnail toggle was off
    when discovered) are silently omitted from the response. Missing
    keys on the client side render the placeholder layout.
    """
    raw_ids = body.get("video_ids") or []
    if not isinstance(raw_ids, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="video_ids must be a list of strings.",
        )
    # Strip duplicates + non-strings, cap at 500 so a runaway client
    # can't ask us to presign tens of thousands of URLs in one call.
    video_ids = [v for v in dict.fromkeys(raw_ids) if isinstance(v, str)][:500]
    if not video_ids:
        return {"urls": {}}

    rows = (
        db.query(UserChannelVideo)
        .filter(
            UserChannelVideo.user_id == current.id,
            UserChannelVideo.channel_id == channel_id,
            UserChannelVideo.video_id.in_(video_ids),
        )
        .all()
    )
    urls: Dict[str, str] = {}
    for r in rows:
        if not r.thumbnail_r2_key:
            continue
        try:
            urls[r.video_id] = r2.presign_get(
                r.thumbnail_r2_key, expires_in=3600, subject=current.id
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "presign_get failed for thumbnail %s", r.thumbnail_r2_key
            )
    return {"urls": urls}


@router.get("/videos/{video_id}/download-url")
def get_video_download_url(
    video_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return a short-lived signed R2 URL the user can fetch the .mp4 from.

    The browser does the actual download — bytes don't pass through our
    origin (R2 has free egress + lower latency from Cloudflare's edge).
    The URL is scoped to a single video and expires in 5 minutes; the
    user can request a fresh one any time.
    """
    rows = (
        db.query(UserChannelVideo)
        .filter(
            UserChannelVideo.user_id == current.id,
            UserChannelVideo.video_id == video_id,
        )
        .all()
    )
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video not found.",
        )

    # Use the first matching row; if a video lives in multiple channels
    # for the user, they all share the same R2 object anyway.
    try:
        data = json.loads(rows[0].data_json)
    except json.JSONDecodeError:
        data = {}
    r2_key = data.get("localPath")
    if not r2_key:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This video isn't archived yet — sync it first.",
        )

    try:
        url = r2.presign_get(r2_key, expires_in=300, subject=current.id, proxy=True)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Couldn't generate download URL.",
        )

    title = data.get("title") or video_id
    safe = _sanitize_filename(title, video_id)

    return {
        "url": url,
        "filename": f"{safe}.mp4",
        "fileSizeBytes": data.get("fileSizeBytes"),
        "expiresInSec": 300,
    }


@router.get("/videos/{video_id}/field-history")
def get_video_field_history(
    video_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return the full historical record of versioned metadata for a video.

    Response shape (per field):
      {
        "title": {
          "current": {
            "value": "...",
            "since": <ISO captured_at of last change, or archivedAt if never changed>,
            "lastConfirmedAt": <ISO last_metadata_sync_at, or null>,
          },
          "history": [
            {
              "value": "...",
              "capturedAt": ISO,
              "lastSeenAt": ISO,
              "supersededAt": ISO,
              ...field-specific extras...
            },
            ...ordered most-recent-first
          ]
        },
        "description": { ... },
        ...
      }

    For thumbnail snapshots, each history entry also has a `downloadUrl`
    (short-lived presigned R2 URL) so the user can open or save any past
    thumbnail.
    """
    rows = (
        db.query(UserChannelVideo)
        .filter(
            UserChannelVideo.user_id == current.id,
            UserChannelVideo.video_id == video_id,
        )
        .all()
    )
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video not found.",
        )
    row = rows[0]
    try:
        data = json.loads(row.data_json)
    except json.JSONDecodeError:
        data = {}

    snapshots = (
        db.query(VideoFieldSnapshot)
        .filter(
            VideoFieldSnapshot.user_id == current.id,
            VideoFieldSnapshot.video_id == video_id,
        )
        .order_by(VideoFieldSnapshot.superseded_at.desc())
        .all()
    )

    # Bucket snapshots by field for the per-field response shape.
    by_field: Dict[str, list] = {}
    for snap in snapshots:
        by_field.setdefault(snap.field, []).append(snap)

    # Compute "since" for the current value: most recent snapshot's
    # superseded_at if any exist (that's when the current value
    # replaced the old one); otherwise the archive time as a fallback.
    def _current_since(field: str) -> Optional[str]:
        if by_field.get(field):
            return by_field[field][0].superseded_at.isoformat()
        return data.get("archivedAt")

    last_confirmed = (
        row.last_metadata_sync_at.isoformat()
        if row.last_metadata_sync_at
        else None
    )

    def _serialize_entry(snap: "VideoFieldSnapshot") -> Dict[str, Any]:
        try:
            value = json.loads(snap.value_json)
        except json.JSONDecodeError:
            value = None
        entry: Dict[str, Any] = {
            "value": value,
            "capturedAt": snap.captured_at.isoformat(),
            "lastSeenAt": snap.last_seen_at.isoformat(),
            "supersededAt": snap.superseded_at.isoformat(),
        }
        # Thumbnail entries: presign their R2 key so the frontend can
        # display + offer download without minting URLs separately.
        if snap.field == "thumbnail" and snap.r2_key:
            try:
                entry["downloadUrl"] = r2.presign_get(snap.r2_key, expires_in=300, subject=current.id)
            except Exception:
                entry["downloadUrl"] = None
        return entry

    result: Dict[str, Any] = {}
    for field in ("title", "description", "tags", "thumbnail", "privacy"):
        current_value: Any
        if field == "thumbnail":
            current_value = {"url": data.get("thumbnailUrl")}
        else:
            current_value = data.get(field)
        result[field] = {
            "current": {
                "value": current_value,
                "since": _current_since(field),
                "lastConfirmedAt": last_confirmed,
            },
            "history": [_serialize_entry(s) for s in by_field.get(field, [])],
        }
    return result


@router.get("/videos/{video_id}/comments")
def get_video_comments(
    video_id: str,
    sort: str = "new",
    include_deleted: bool = True,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """List archived comments for a single video.

    Query params:
      sort:            "new"     -> newest first (default; what archiving uses)
                       "top"     -> highest like_count first
                       "deleted" -> only soft-deleted, most-recently-deleted first
      include_deleted: when True, deleted comments appear inline with
                       a `deletedAt` timestamp. When False they're filtered out.
                       Ignored when sort=="deleted" (that mode always shows
                       only deleted).
      limit / offset:  classic pagination.

    Comments are flat (top-level + reply) - the parentCommentId lets
    callers group into threads client-side. We don't return any text
    history beyond the current snapshot here; that's a separate
    feature.
    """
    # Validate the video belongs to the requesting user before we touch
    # the comments table.
    video_owned = (
        db.query(UserChannelVideo)
        .filter(
            UserChannelVideo.user_id == current.id,
            UserChannelVideo.video_id == video_id,
        )
        .first()
    )
    if video_owned is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video not found.",
        )

    q = db.query(VideoComment).filter(
        VideoComment.user_id == current.id,
        VideoComment.video_id == video_id,
    )

    if sort == "deleted":
        q = q.filter(VideoComment.deleted_at.is_not(None))
        q = q.order_by(VideoComment.deleted_at.desc())
    else:
        if not include_deleted:
            q = q.filter(VideoComment.deleted_at.is_(None))
        if sort == "top":
            q = q.order_by(VideoComment.like_count.desc())
        else:
            # Default: "new" - newest comments first by publish time,
            # falling back to first_seen_at when YouTube didn't give
            # us a publish timestamp.
            q = q.order_by(
                VideoComment.published_at.desc().nulls_last(),
                VideoComment.first_seen_at.desc(),
            )

    total = q.count()
    rows = q.offset(offset).limit(limit).all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "comments": [_serialize_comment(r) for r in rows],
    }


def _serialize_comment(c: VideoComment) -> Dict[str, Any]:
    return {
        "id": c.id,
        "parentCommentId": c.parent_comment_id,
        "videoId": c.video_id,
        "author": c.author,
        "authorChannelId": c.author_channel_id,
        "text": c.text,
        "likeCount": c.like_count,
        "isEdited": c.is_edited,
        "isPinned": c.is_pinned,
        "isByUploader": c.is_by_uploader,
        "viewerRatingLike": c.viewer_rating_like,
        "publishedAt": c.published_at.isoformat() if c.published_at else None,
        "updatedAtRemote": c.updated_at_remote.isoformat() if c.updated_at_remote else None,
        "firstSeenAt": c.first_seen_at.isoformat(),
        "lastSeenAt": c.last_seen_at.isoformat(),
        "deletedAt": c.deleted_at.isoformat() if c.deleted_at else None,
    }


@router.get("/channels/{channel_id}/comments/recently-deleted")
def get_channel_recently_deleted_comments(
    channel_id: str,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Channel-wide feed of comments that have been soft-deleted, most
    recently first.

    This is the headline feature of the comments archive - what the
    user is paying to preserve. We include the video_id on each so
    the UI can group + link out.
    """
    q = (
        db.query(VideoComment)
        .filter(
            VideoComment.user_id == current.id,
            VideoComment.channel_id == channel_id,
            VideoComment.deleted_at.is_not(None),
        )
        .order_by(VideoComment.deleted_at.desc())
    )
    total = q.count()
    rows = q.offset(offset).limit(limit).all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "comments": [_serialize_comment(r) for r in rows],
    }


@router.get("/channels/{channel_id}/comments/search")
def search_channel_comments(
    channel_id: str,
    q: str = "",
    sort: str = "new",
    include_deleted: bool = True,
    only_deleted: bool = False,
    author_channel_id: Optional[str] = None,
    video_id: Optional[str] = None,
    min_likes: int = 0,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Search + filter the user's archived comments for one channel.

    Cheap LIKE-based text search for the MVP. Once volume warrants it
    we can move to SQLite FTS5 with no API contract change.

    Filters:
      q                 substring match on the comment body (case-insensitive)
      author_channel_id only comments by this commenter
      video_id          only comments on this video
      only_deleted      shorthand for "show me only deletions"
      include_deleted   default true - flip false to hide deletions
      min_likes         only comments with likeCount >= this value
      sort              "new" | "top" | "deleted"
    """
    query = db.query(VideoComment).filter(
        VideoComment.user_id == current.id,
        VideoComment.channel_id == channel_id,
    )

    if q:
        like = f"%{q}%"
        query = query.filter(VideoComment.text.ilike(like))
    if author_channel_id:
        query = query.filter(VideoComment.author_channel_id == author_channel_id)
    if video_id:
        query = query.filter(VideoComment.video_id == video_id)
    if only_deleted:
        query = query.filter(VideoComment.deleted_at.is_not(None))
    elif not include_deleted:
        query = query.filter(VideoComment.deleted_at.is_(None))
    if min_likes > 0:
        query = query.filter(VideoComment.like_count >= min_likes)

    if sort == "top":
        query = query.order_by(VideoComment.like_count.desc())
    elif sort == "deleted":
        query = query.filter(VideoComment.deleted_at.is_not(None))
        query = query.order_by(VideoComment.deleted_at.desc())
    else:
        query = query.order_by(
            VideoComment.published_at.desc().nulls_last(),
            VideoComment.first_seen_at.desc(),
        )

    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "comments": [_serialize_comment(r) for r in rows],
    }


# Characters that actually break on at least one major filesystem.
# Everything else (apostrophes, parens, brackets, commas, emoji, etc.)
# is fine to keep - macOS, Linux, and modern Windows all accept them
# in filenames. Earlier we were using an alphanumeric-only allowlist
# which was way too aggressive and turned "Azeem Ratnani's wedding"
# into "Azeem Ratnani_s wedding".
_FORBIDDEN_FILENAME_CHARS = set('/\\:*?"<>|\0')


def _sanitize_filename(s: str, fallback: str) -> str:
    """Make a string safe to use as a filename across major OSes
    while keeping it human-readable.

    Strips only characters that are genuinely problematic on at
    least one filesystem (path separators, Windows reserved chars,
    NUL, ASCII control codes). Apostrophes, parentheses, commas,
    emoji and the like are passed through.

    Also trims leading/trailing whitespace and dots - Windows
    quietly mangles filenames that end in a dot.
    """
    cleaned_chars = []
    for c in s:
        if c in _FORBIDDEN_FILENAME_CHARS:
            continue
        if ord(c) < 32:
            # ASCII control characters (incl. tab/newline/CR)
            continue
        cleaned_chars.append(c)
    cleaned = "".join(cleaned_chars).strip(" .\t")
    return cleaned or fallback


@router.get("/videos/{video_id}/download-parts")
def get_video_download_parts(
    video_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return everything the user might want to save for one video.

    Powers the side-panel download picker (Video / Thumbnail / Metadata
    checkboxes). Each presigned R2 URL bakes in a Content-Disposition
    so the browser saves the file with the video's actual title rather
    than the raw R2 key. Metadata is returned inline as a JSON object -
    the frontend either saves it as metadata.json on its own or packs
    it into a ZIP alongside the other selected parts.
    """
    rows = (
        db.query(UserChannelVideo)
        .filter(
            UserChannelVideo.user_id == current.id,
            UserChannelVideo.video_id == video_id,
        )
        .all()
    )
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video not found.",
        )
    row = rows[0]
    try:
        data = json.loads(row.data_json)
    except json.JSONDecodeError:
        data = {}

    title = data.get("title") or video_id
    safe_title = _sanitize_filename(title, video_id)

    # Channel context for the metadata payload.
    channel_row = db.get(UserChannel, (current.id, row.channel_id))
    channel_payload: Dict[str, Any] = {}
    if channel_row is not None:
        try:
            channel_data = json.loads(channel_row.data_json)
        except json.JSONDecodeError:
            channel_data = {}
        channel_payload = {
            "id": row.channel_id,
            "handle": channel_data.get("handle"),
            "name": channel_data.get("name"),
        }

    # Build the metadata document (returned inline; not in R2 - it's
    # a derived view of data_json). Keep this stable - users may pipe
    # it into their own tooling.
    # Comments + comment count are intentionally absent from this
    # metadata export. Per the product direction, comments are their
    # own dedicated sync surface (a third option alongside Videos and
    # Metadata on the sync panel) and their archive shape will be
    # separate from the general video metadata bundle.
    metadata: Dict[str, Any] = {
        "id": video_id,
        "title": data.get("title"),
        "description": data.get("description"),
        "uploadDate": data.get("uploadDate"),
        "durationSec": data.get("durationSec"),
        "viewCount": data.get("viewCount"),
        "tags": data.get("tags") or [],
        "privacy": data.get("privacy"),
        "type": data.get("type"),
        "captionLanguages": data.get("captionLanguages") or [],
        "archivedAt": data.get("archivedAt"),
        "firstSeenAt": data.get("firstSeenAt"),
        "thumbnailUrlOriginal": data.get("thumbnailUrl"),
        "youtubeUrl": f"https://www.youtube.com/watch?v={video_id}",
        "channel": channel_payload,
    }

    video_url: Optional[str] = None
    video_size_bytes: Optional[int] = data.get("fileSizeBytes")
    video_key = data.get("localPath")
    if video_key:
        try:
            video_url = r2.presign_get(
                video_key,
                expires_in=300,
                filename=f"{safe_title}.mp4",
                content_type="video/mp4",
                subject=current.id,
                proxy=True,
            )
        except Exception:
            log.exception("download-parts: presign video failed")
            video_url = None

    # Generic 'thumbnail.jpg' name regardless of video title. Inside a
    # ZIP that's already titled after the video, an extra title-based
    # name is redundant; standalone it's still clear what the file is.
    thumbnail_filename = "thumbnail.jpg"
    thumbnail_url: Optional[str] = None
    if row.thumbnail_r2_key:
        try:
            thumbnail_url = r2.presign_get(
                row.thumbnail_r2_key,
                expires_in=300,
                filename=thumbnail_filename,
                content_type="image/jpeg",
                subject=current.id,
                proxy=True,
            )
        except Exception:
            log.exception("download-parts: presign thumbnail failed")
            thumbnail_url = None

    # Captions - presigned per-language URLs the frontend can either
    # stuff straight into the ZIP under captions/{lang}.vtt or save as
    # standalone files. We only return entries whose R2 PUT actually
    # landed (worker reports back via captionLanguages on /complete).
    # Derive the captions directory from the video's stored localPath
    # so old (videos/{vid}/) and new (users/{uid}/videos/{vid}/) layouts
    # both resolve correctly.
    caption_parts = []
    video_local_path = (data.get("localPath") or "").strip()
    captions_base = r2_paths.captions_base_for_video(video_local_path)
    for lang in data.get("captionLanguages") or []:
        if not isinstance(lang, str) or not lang or not captions_base:
            continue
        key = f"{captions_base}/captions/{lang}.vtt"
        try:
            cap_url = r2.presign_get(
                key,
                expires_in=300,
                filename=f"{lang}.vtt",
                content_type="text/vtt",
                subject=current.id,
                proxy=True,
            )
        except Exception:
            log.exception("download-parts: presign caption %s failed", lang)
            continue
        caption_parts.append(
            {
                "language": lang,
                "url": cap_url,
                "filename": f"{lang}.vtt",
            }
        )

    return {
        "title": title,
        "safeTitle": safe_title,
        "video": {
            "url": video_url,
            "filename": f"{safe_title}.mp4",
            "sizeBytes": video_size_bytes,
            "available": video_url is not None,
        },
        "thumbnail": {
            "url": thumbnail_url,
            "filename": thumbnail_filename,
            "available": thumbnail_url is not None,
        },
        "metadata": {
            "filename": f"{safe_title}.json",
            "available": True,
            "data": metadata,
        },
        "captions": caption_parts,
        "expiresInSec": 300,
    }


def _channel_settings_subscription(
    db: Session, user_id: str, channel_youtube_id: str
) -> Optional[UserChannelSubscription]:
    """The shared-pool subscription row that holds this channel's settings."""
    channel = (
        db.query(Channel)
        .filter(Channel.youtube_id == channel_youtube_id)
        .one_or_none()
    )
    if channel is None:
        return None
    return (
        db.query(UserChannelSubscription)
        .filter(
            UserChannelSubscription.user_id == user_id,
            UserChannelSubscription.channel_id == channel.id,
            UserChannelSubscription.unsubscribed_at.is_(None),
        )
        .one_or_none()
    )


def _effective_channel_settings(
    db: Session, user_id: str, channel_youtube_id: str, legacy: Dict[str, Any]
) -> Dict[str, Any]:
    """What this channel is ACTUALLY set to, as the frontend would see it.

    Reads are served from the subscription row (see
    archive.channel_response_payload), so that row is the answer to "what
    is this channel set to". The legacy UserChannel copy is a mirror, and
    a mirror that has drifted describes what the channel used to be.
    """
    sub = _channel_settings_subscription(db, user_id, channel_youtube_id)
    if sub is not None and sub.settings_json:
        try:
            stored = json.loads(sub.settings_json)
        except (TypeError, ValueError):
            stored = None
        if isinstance(stored, dict) and stored:
            return stored
    return legacy


def _store_channel_settings(
    db: Session,
    user_id: str,
    channel_youtube_id: str,
    legacy_row: UserChannel,
    settings: Dict[str, Any],
) -> None:
    """Write per-channel settings to BOTH rows that hold them.

    Settings live in two places - legacy UserChannel.data_json["settings"]
    and shared-pool UserChannelSubscription.settings_json - and every read
    serves the second one. A write that updates only the legacy row is
    therefore invisible: it stores, it commits, and it verifies correctly
    against the table you would think to check, while the user sees
    nothing change. That is exactly what /settings/reset did, and the
    wrong-table verification is what made it look like a display bug.
    One function, so there is one place that has to remember.
    """
    try:
        data = json.loads(legacy_row.data_json)
    except (TypeError, ValueError):
        data = {}
    data["settings"] = settings
    legacy_row.data_json = json.dumps(data)
    legacy_row.updated_at = datetime.now(timezone.utc)

    sub = _channel_settings_subscription(db, user_id, channel_youtube_id)
    if sub is not None:
        sub.settings_json = json.dumps(settings)


def _require_channel_active(db: Session, user_id: str, channel_id: str) -> None:
    """Refuse any billable work on a paused channel.

    The Active switch is a SPENDING control, not a sync preference. It
    exists so someone can add a channel, configure it, authenticate it
    and look around without being charged a cent, then turn it on when
    they are ready to start paying. Every route that creates stored
    bytes therefore has to honour it: videos, captions, comments and
    per-video metadata all land in the bucket and all appear on the bill.

    The automatic paths (nightly sweep, upload notification) have always
    checked, via auto_download_enabled. The manual endpoints did not, and
    the Sync button quietly queued 498 videos onto a channel the owner
    had switched off - roughly 115 GB he was about to be billed for.

    Read through _effective_channel_settings so the subscription row
    decides, because that is the row every read is served from; a stale
    legacy copy saying "active" must not open the gate.
    """
    legacy = db.get(UserChannel, (user_id, channel_id))
    if legacy is None:
        return  # not tracked by this user; the caller's own 404 applies
    try:
        stored = (json.loads(legacy.data_json) or {}).get("settings") or {}
    except (TypeError, ValueError):
        stored = {}
    settings = _effective_channel_settings(db, user_id, channel_id, stored)
    if not settings.get("active", True):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This channel is paused, so nothing will sync. Turn it on "
                "in the channel's settings first."
            ),
        )


def _handle_from_input(raw: str) -> str:
    """The @handle the user actually typed, if they typed one.

    Matches "@AFRFX", "youtube.com/@AFRFX", "youtube.com/@AFRFX/videos".
    Returns "" for a bare UC id or a /channel/UC… url, where there is no
    handle in the string to read.
    """
    m = re.search(r"@([A-Za-z0-9._\-]+)", raw or "")
    return f"@{m.group(1)}" if m else ""


@router.post("/channels/{channel_id}/settings/reset")
def reset_channel_settings(
    channel_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Re-apply the user's New-channel-defaults to an existing channel.

    Account defaults used to be applied exactly once, when a channel was
    added, and never again - so changing them left every channel you
    already had wearing whatever it was created with, and the only way to
    re-apply them was to remove the channel and add it back. Someone did
    exactly that, then spent an afternoon working out why the settings
    still looked wrong. This is that operation, without the round trip.

    `active` is deliberately preserved. It is the channel's on/off switch
    rather than a preference, and quietly pausing a channel because
    somebody asked to tidy their metadata toggles would be a surprise of
    the worst kind - the sort that stops backups without saying so.
    """
    row = db.get(UserChannel, (current.id, channel_id))
    if row is None or row.removed_at is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found."
        )

    try:
        data = json.loads(row.data_json)
    except (TypeError, ValueError):
        data = {}
    # The settings actually in force, not the legacy mirror. Diffing the
    # mirror would reconcile metadata jobs against settings the channel
    # stopped running under, and preserve an `active` the user never set.
    previous = _effective_channel_settings(
        db, current.id, channel_id, data.get("settings") or {}
    )

    fresh = _new_channel_settings(db, current.id)
    fresh["active"] = previous.get("active", True)

    _store_channel_settings(db, current.id, channel_id, row, fresh)

    # Metadata toggles drive real background work (thumbnail capture,
    # history snapshots), so the same reconciliation the settings PUT does
    # has to run here too - otherwise the stored settings and the jobs
    # actually queued would disagree.
    _apply_video_metadata_toggles(
        db, current.id, channel_id, fresh, prev_settings=previous
    )
    db.commit()
    return {"settings": fresh}


@router.delete("/channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_channel(
    channel_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Response:
    """Soft-delete a channel from the user's list. ``channel_id`` is
    the YouTube UC id.

    Shared-pool: sets unsubscribed_at on the user's
    UserChannelSubscription — that's the row list_channels reads, so
    it's what actually makes the channel disappear, and it stops the v2
    storage meter for this user. The shared Channel + every Video row
    stay in the DB (other users may still subscribe). Re-adding inside
    the 30-day grace window reuses the data; after that the purge cron
    hard-deletes orphaned rows + R2 keys.

    The legacy UserChannel.removed_at + storage-ledger propagation are
    kept in sync for any code still reading the old tables.

    No-op if already removed - returning 204 either way is friendlier
    than 404 if the user double-clicks.
    """
    from app import archive as archive_lib  # noqa: WPS433
    from app.models import Channel as _Channel  # noqa: WPS433

    # New shared-pool model: resolve the Channel by its YouTube id and
    # soft-delete THIS user's subscription. list_channels reads from
    # here, so this is the part that fixes "removed channel still shows".
    channel = (
        db.query(_Channel)
        .filter(_Channel.youtube_id == channel_id)
        .one_or_none()
    )
    if channel is not None:
        archive_lib.soft_delete_subscription(db, current.id, channel.id)

        # The hub subscription is per-Channel (shared across every
        # subscriber), so we only tear it down when the LAST active
        # subscriber leaves. Otherwise we keep getting upload
        # notifications for a channel nobody tracks — orphan Video rows
        # + wasted renewal work that grows over time.
        remaining = (
            db.query(UserChannelSubscription)
            .filter(
                UserChannelSubscription.channel_id == channel.id,
                UserChannelSubscription.unsubscribed_at.is_(None),
            )
            .count()
        )
        if remaining == 0:
            try:
                pubsub.unsubscribe_channel(channel.youtube_id)
            except Exception:
                # Best-effort: an un-acked unsubscribe just means the
                # hub lease lapses on its own (10-day max), and the
                # renewal cron now skips zero-subscriber channels so it
                # won't be renewed. Clear our lease columns either way.
                log.exception(
                    "pubsub unsubscribe failed for %s", channel.youtube_id
                )
            channel.pubsub_lease_expires_at = None
            channel.pubsub_last_renewed_at = None

    # Legacy model: keep removed_at + the storage-ledger propagation in
    # sync so anything still reading the old tables agrees.
    legacy = db.get(UserChannel, (current.id, channel_id))
    if legacy is not None and legacy.removed_at is None:
        legacy.removed_at = datetime.now(timezone.utc)
        # R2 objects stay in place until the daily purge cron drops
        # them; we eat that cost during the grace window deliberately.
        storage_ledger.propagate_channel_soft_delete(
            db,
            current.id,
            channel_id,
            removed_at=legacy.removed_at,
        )

    # Removing a channel cancels its UNSTARTED work. Nothing else does -
    # the claim query filters on user_id and status only - so without this
    # a removed channel keeps downloading onto a bill the user believes
    # they stopped. Invisible at three videos; at a 20,000-video back
    # catalogue it is months of downloads they explicitly cancelled.
    #
    # Only 'pending'. A job already running is left to finish and record
    # its bytes: abandoning it mid-flight is exactly how an object lands
    # in Backblaze with no ledger row.
    #
    # Marked failed rather than deleted - server-side rows are never
    # destroyed here, and the purge cron owns real deletion.
    db.query(SyncJob).filter(
        SyncJob.user_id == current.id,
        SyncJob.channel_id == channel_id,
        SyncJob.status == "pending",
    ).update(
        {
            "status": "failed",
            "error": _CANCELLED_CHANNEL_REMOVED,
            "finished_at": datetime.now(timezone.utc),
        },
        synchronize_session=False,
    )

    # 404 only when the channel is unknown in BOTH models — otherwise a
    # subscription-only (post-legacy) channel would wrongly 404.
    if channel is None and legacy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Channel not found.",
        )

    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------- Sync jobs (worker queue) ----------


@router.post("/channels/{channel_id}/sync-files")
def enqueue_sync_files(
    channel_id: str,
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
    current: User = Depends(get_paid_user),
) -> Dict[str, Any]:
    """Queue one sync job per requested video.

    Request body: ``{"video_ids": ["abc", "def", ...]}``. We enqueue a job
    only for videos the user actually has discovered for this channel
    (anything else is silently dropped to avoid leaking existence info).
    Videos already pending or running for this user are skipped — no
    duplicate work.
    """
    video_ids = payload.get("video_ids") or []
    if not isinstance(video_ids, list) or not video_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="video_ids must be a non-empty list.",
        )

    _require_channel_active(db, current.id, channel_id)

    # Restrict to videos the user actually owns on this channel
    owned = {
        r.video_id
        for r in db.query(UserChannelVideo.video_id)
        .filter(
            UserChannelVideo.user_id == current.id,
            UserChannelVideo.channel_id == channel_id,
            UserChannelVideo.video_id.in_(video_ids),
        )
        .all()
    }

    # Drop videos that already have an in-flight job
    in_flight = {
        r.video_id
        for r in db.query(SyncJob.video_id)
        .filter(
            SyncJob.user_id == current.id,
            SyncJob.channel_id == channel_id,
            SyncJob.video_id.in_(owned),
            SyncJob.status.in_(["pending", "running"]),
        )
        .all()
    }

    # Order jobs oldest-upload-first so the worker grinds through the
    # user's catalog in chronological order. Previously these were
    # alphabetical-by-video-id which felt random. Falls back to empty
    # string when uploadDate is missing - those sort first, which is
    # fine (they're typically older anyway).
    candidate_ids = owned - in_flight
    rows_for_dates = (
        db.query(UserChannelVideo)
        .filter(
            UserChannelVideo.user_id == current.id,
            UserChannelVideo.channel_id == channel_id,
            UserChannelVideo.video_id.in_(candidate_ids),
        )
        .all()
    )

    def _upload_date(row: UserChannelVideo) -> str:
        try:
            return json.loads(row.data_json).get("uploadDate") or ""
        except json.JSONDecodeError:
            return ""

    def _status(row: UserChannelVideo) -> str:
        try:
            return json.loads(row.data_json).get("status") or ""
        except json.JSONDecodeError:
            return ""

    # Look up the channel's current quality settings once - we use them
    # to decide whether an already-archived video should be allowed
    # through as an "outdated" re-archive.
    ch_row = db.get(UserChannel, (current.id, channel_id))
    current_resolution: Optional[str] = None
    current_codec: Optional[str] = None
    if ch_row is not None:
        try:
            ch_settings = json.loads(ch_row.data_json).get("settings") or {}
            current_resolution = ch_settings.get("maxResolution")
            current_codec = ch_settings.get("codecPreference")
        except json.JSONDecodeError:
            pass

    def _is_outdated(r: UserChannelVideo) -> bool:
        """An archived video is outdated when its captured quality
        settings don't match the channel's current settings. Matches
        the frontend's isQualityOutdated() definition exactly: any
        mismatch (including downgrades) counts."""
        try:
            d = json.loads(r.data_json)
        except json.JSONDecodeError:
            return False
        archived_res = d.get("archivedMaxResolution")
        archived_codec = d.get("archivedCodecPreference")
        # Pre-feature archives without the stamped fields are NOT flagged
        # outdated - we don't know what quality they're at and shouldn't
        # silently re-download them.
        if not archived_res or not archived_codec:
            return False
        if current_resolution and archived_res != current_resolution:
            return True
        if current_codec and archived_codec != current_codec:
            return True
        return False

    # Two skip categories:
    #   - already archived AND at current quality -> skip (no work needed)
    #   - already archived AND outdated -> ALLOW (bulk re-archive flow)
    # Plus the normal "discovered" / "failed" flow which goes through.
    skipped_already_archived = 0
    eligible_rows: List[UserChannelVideo] = []
    for r in rows_for_dates:
        if _status(r) == "archived":
            if _is_outdated(r):
                eligible_rows.append(r)
            else:
                skipped_already_archived += 1
        else:
            eligible_rows.append(r)

    to_enqueue = [
        r.video_id for r in sorted(eligible_rows, key=_upload_date)
    ]
    created_ids: List[str] = []
    for vid in to_enqueue:
        job = SyncJob(
            user_id=current.id,
            channel_id=channel_id,
            video_id=vid,
        )
        db.add(job)
        db.flush()
        created_ids.append(job.id)
    db.commit()

    return {
        "enqueued": len(created_ids),
        "skipped_in_flight": len(in_flight),
        "skipped_already_archived": skipped_already_archived,
        "skipped_unknown": len(video_ids) - len(owned),
        "job_ids": created_ids,
    }


@router.post("/worker/revocations/ack")
def acknowledge_revocations(
    payload: Optional[Dict[str, Any]] = Body(default=None),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """The worker confirms it finished signing out of revoked channels.

    Body: ``{"channels": [youtube channel ids]}``.

    This is what collapses the website's transient "disconnected" state
    into the plain connect state. Clearing ``user_revoked_at`` here - and
    ONLY here - is the simpler model the owner chose: once the app has
    genuinely dropped the credentials, the standing block has nothing left
    to block, so signing back into the worker later re-authorizes without a
    separate website step.

    Deliberately an explicit acknowledgment rather than an inference from a
    channel going missing in a connection report: a transient probe failure
    looks identical to a completed sign-out in those reports, and inferring
    from absence would lift a user's block while the credentials still
    exist, letting the next healthy report silently undo their disconnect.

    ``revoked_at`` stays set. Authorization returns only when a real
    sign-in report re-proves ownership, which ensure_ownership permits once
    the sticky flag is gone.

    Known trade, accepted with the model: one Google login can own several
    channels, so signing into that login later for ANY of them re-reports
    them all and re-authorizes this one too. The block used to prevent
    that; the owner chose sign-in-means-reconnect instead.
    """
    channels = (payload or {}).get("channels")
    if not isinstance(channels, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Body must be {\"channels\": [...]}.",
        )
    cleared: List[str] = []
    for youtube_id in channels:
        if not isinstance(youtube_id, str) or not youtube_id:
            continue
        channel = (
            db.query(Channel).filter(Channel.youtube_id == youtube_id).one_or_none()
        )
        if channel is None:
            continue
        own = (
            db.query(ChannelOwnership)
            .filter(
                ChannelOwnership.user_id == current.id,
                ChannelOwnership.channel_id == channel.id,
            )
            .one_or_none()
        )
        if own is None or own.user_revoked_at is None:
            continue
        own.user_revoked_at = None
        cleared.append(youtube_id)
    if cleared:
        db.commit()
        log.info(
            "worker acknowledged revocations for %s: sticky block cleared",
            cleared,
        )
    return {"cleared": cleared}


@router.put("/worker-connection")
def report_worker_connection(
    payload: Optional[Dict[str, Any]] = Body(default=None),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """A Basic user's own worker app reports its YouTube connection state.

    The connection lives in the app's local embedded webview (cookies), so the
    backend can't observe it directly. The website's Connections tab mirrors
    this for Basic users. Body:
    ``{"connected": bool, "cookieCount": int, "channelTitle": str?}``

    Answers with ``revokedChannels``: the channels this user has withdrawn
    worker access to. Revoking used to stop only the SERVER honouring the
    worker's ownership claim - the worker kept its cookies and kept
    reporting itself connected, so the website said "revoked" while the app
    said "connected" about the same account. The worker treats a channel
    appearing here as an instruction to disconnect that account locally.
    Older workers ignore the key, which is exactly the pre-existing
    behaviour, so nothing regresses by adding it.
    """
    data = payload or {}
    row = db.get(WorkerYoutubeConnection, current.id)
    if row is None:
        row = WorkerYoutubeConnection(user_id=current.id)
        db.add(row)
    row.connected = bool(data.get("connected"))
    row.cookie_count = int(data.get("cookieCount") or 0)
    title = data.get("channelTitle")
    if isinstance(title, str) and title.strip():
        row.channel_title = title.strip()
    row.reported_at = datetime.now(timezone.utc)

    # The worker also reports which channels its connected logins ARE (the
    # channel id read from each account's account_menu). Being signed in as
    # a channel's Google account is proof of ownership, so record it - that's
    # what unlocks the channel's sealed (private / members-only) videos.
    # Only channels we already know about (the user tracked them) are
    # recorded; unknown ids are ignored.
    owned = data.get("ownedChannels")
    if isinstance(owned, list):
        from app import archive as archive_lib  # noqa: WPS433
        from app import auto_download  # noqa: WPS433

        for cid in owned:
            if not isinstance(cid, str) or not cid.strip():
                continue
            channel = (
                db.query(Channel)
                .filter(Channel.youtube_id == cid.strip())
                .one_or_none()
            )
            if channel is not None:
                # Whether this report is the moment ownership STARTS, as
                # opposed to the worker re-stating it on every launch.
                # Only the transition forgives history; re-running it
                # every launch would hand a genuinely private video five
                # fresh attempts each time the app opened.
                prior = (
                    db.query(ChannelOwnership)
                    .filter(
                        ChannelOwnership.user_id == current.id,
                        ChannelOwnership.channel_id == channel.id,
                    )
                    .one_or_none()
                )
                newly_owned = prior is None or prior.revoked_at is not None
                archive_lib.ensure_ownership(
                    db, current.id, channel.id, google_user_id="worker"
                )
                if newly_owned:
                    # Videos refused for lack of credentials were counted
                    # against themselves. We have credentials now, so that
                    # tally is about a world that no longer exists.
                    auto_download.forgive_permission_failures(
                        db,
                        user_id=current.id,
                        channel_youtube_id=channel.youtube_id,
                    )

    db.commit()
    return {
        "ok": True,
        "revokedChannels": _user_revoked_channel_youtube_ids(db, current.id),
    }


@router.get("/worker/tracked-channels")
def worker_tracked_channels(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Every channel the user tracks on the website, with its auth state.

    This is the worker's channel list, and the website is its only source.
    The worker used to work the other way round - sign in to a Google
    account, enumerate whatever channels that account owned, and push the
    ids up - which let a user "connect" a channel the website had never
    heard of. Nothing happened when they did: PUT /worker-connection looks
    each id up in the shared Channel pool and silently drops the ones it
    does not know (see the ownedChannels loop above), so the app went
    all-green while backing up nothing, and the website showed no trace.
    Worse, whether it silently worked depended on whether some unrelated
    user already tracked that channel, since the pool is shared - the same
    action had two different outcomes decided by a stranger's account.

    Driving the list from here makes that state unreachable: a channel the
    worker cannot see is a channel you have not added yet, and the fix is
    on the website where tracking (and billing) actually starts.

    ``authenticated`` is per-channel and is what unlocks that channel's
    private/unlisted uploads. ``revoked`` is the positive signal that the
    user withdrew worker access - distinct from simply never having
    authenticated, because the worker must drop a stored login for the
    former and merely offer to sign in for the latter.
    """
    rows = (
        db.query(UserChannel)
        .filter(
            UserChannel.user_id == current.id,
            UserChannel.removed_at.is_(None),
        )
        .order_by(UserChannel.added_at.asc())
        .all()
    )
    if not rows:
        return {"channels": []}

    youtube_ids = [r.channel_id for r in rows]
    # Ownership is keyed by the INTERNAL Channel.id, never the UC id the
    # worker speaks, so resolve the pool rows first.
    pool = {
        c.youtube_id: c
        for c in db.query(Channel)
        .filter(Channel.youtube_id.in_(youtube_ids))
        .all()
    }
    authed_pks = {
        pk
        for (pk,) in db.query(ChannelOwnership.channel_id).filter(
            ChannelOwnership.user_id == current.id,
            ChannelOwnership.revoked_at.is_(None),
        )
    }
    revoked_ids = set(_user_revoked_channel_youtube_ids(db, current.id))

    out: List[Dict[str, Any]] = []
    for row in rows:
        try:
            data = json.loads(row.data_json)
        except (TypeError, ValueError):
            data = {}
        channel = pool.get(row.channel_id)
        out.append(
            {
                "youtubeId": row.channel_id,
                "title": data.get("name") or row.channel_id,
                "handle": data.get("handle") or "",
                "thumbnailUrl": data.get("avatarUrl") or "",
                "authenticated": (
                    channel is not None and channel.id in authed_pks
                ),
                "revoked": row.channel_id in revoked_ids,
            }
        )
    return {"channels": out}


@router.get("/worker/owned-channels")
def worker_owned_channels(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Channels the user has authenticated ownership of AND tracks. The
    worker uses these to discover the owner's private/unlisted uploads -
    only the owner's cookies can enumerate those. Returns youtube ids.

    Also carries ``revokedChannels`` - the channels the user has withdrawn
    worker access to. A revoked channel is already absent from ``channels``
    (the ownership filter below drops it), but absence alone reads as "not
    yours" and cannot be distinguished from a channel the user simply
    untracked. The worker needs the positive signal to know it should drop
    that account's login rather than just stop polling for it.
    """
    revoked = _user_revoked_channel_youtube_ids(db, current.id)
    owned = [
        cid
        for (cid,) in db.query(Channel.youtube_id)
        .join(ChannelOwnership, ChannelOwnership.channel_id == Channel.id)
        .filter(
            ChannelOwnership.user_id == current.id,
            ChannelOwnership.revoked_at.is_(None),
        )
        .all()
    ]
    if not owned:
        return {"channels": [], "revokedChannels": revoked}
    tracked = {
        uc.channel_id
        for uc in db.query(UserChannel)
        .filter(
            UserChannel.user_id == current.id,
            UserChannel.channel_id.in_(owned),
            UserChannel.removed_at.is_(None),
        )
        .all()
    }
    return {
        "channels": [c for c in owned if c in tracked],
        "revokedChannels": revoked,
    }


@router.post("/worker/discovered-videos")
def worker_discovered_videos(
    payload: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    current: User = Depends(get_paid_user),
) -> Dict[str, Any]:
    """The worker reports what it found in a tracked channel's uploads
    playlist.

    Anyone tracking the channel may report: enumerating a public uploads
    playlist needs no credentials, and the rows land in the reporter's own
    catalogue and on their own bill. Authenticated owners additionally see
    private and unlisted uploads, and only their reports may record a
    video as private.
    """
    channel_yt = str(payload.get("channelId") or "").strip()
    videos = payload.get("videos") or []
    if not channel_yt or not isinstance(videos, list):
        return {"discovered": 0, "enqueued": 0}
    channel = (
        db.query(Channel).filter(Channel.youtube_id == channel_yt).one_or_none()
    )
    if channel is None:
        return {"discovered": 0, "enqueued": 0}
    # The gate is TRACKING, not ownership.
    #
    # Ownership was being used as an access check when it is really a
    # privacy decision. What has to be true is "this user tracks this
    # channel" - they are reporting their own catalogue into their own
    # rows and onto their own bill. Requiring authenticated ownership
    # meant a user who never signed in to Google could never have
    # anything discovered, so their archive stayed permanently empty even
    # though the entire public catalogue needs no credentials to see.
    tracked = (
        db.query(UserChannel.user_id)
        .filter(
            UserChannel.user_id == current.id,
            UserChannel.channel_id == channel_yt,
            UserChannel.removed_at.is_(None),
        )
        .first()
        is not None
    )
    if not tracked:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You aren't tracking that channel.",
        )

    # Ownership now decides only the privacy TIER we record, never whether
    # the report is accepted. The worker tells us whether it enumerated
    # with credentials; we still verify that claim against our own
    # ownership record rather than trusting the client.
    claimed_auth = bool(payload.get("authenticated"))
    owns = (
        db.query(ChannelOwnership.id)
        .filter(
            ChannelOwnership.user_id == current.id,
            ChannelOwnership.channel_id == channel.id,
            ChannelOwnership.revoked_at.is_(None),
        )
        .first()
        is not None
    )
    enumerated_with_cookies = claimed_auth and owns
    existing = {
        r.video_id
        for r in db.query(UserChannelVideo.video_id)
        .filter(
            UserChannelVideo.user_id == current.id,
            UserChannelVideo.channel_id == channel_yt,
        )
        .all()
    }
    new_ids: List[str] = []
    for v in videos:
        if not isinstance(v, dict):
            continue
        vid = str(v.get("id") or "").strip()
        if not vid or vid in existing:
            continue
        title = str(v.get("title") or vid).strip() or vid
        payload_in = {"id": vid, "title": title}
        upload_date = str(v.get("uploadDate") or "").strip()
        if upload_date:
            payload_in["uploadDate"] = upload_date
        full = _video_template(channel_yt, payload_in)
        # Only a credentialed enumeration can see anything non-public, so
        # only a credentialed enumeration may record something as private.
        #
        # This used to stamp EVERY discovered video "private"
        # unconditionally, which then became `sealed` visibility. On an
        # owner enumerating their own uploads playlist - mostly public
        # videos - that mislabelled the entire public catalogue as
        # owner-only: invisible to every other subscriber of the channel
        # and billed on the wrong tier. An anonymous enumeration can only
        # have seen public videos, so it records public.
        full["privacy"] = "private" if enumerated_with_cookies else "public"
        db.add(
            UserChannelVideo(
                user_id=current.id,
                channel_id=channel_yt,
                video_id=vid,
                data_json=json.dumps(full),
            )
        )
        new_ids.append(vid)
        existing.add(vid)
    db.commit()

    enqueued = 0
    if new_ids:
        result = enqueue_sync_files(
            channel_yt, {"video_ids": new_ids}, db=db, current=current
        )
        if isinstance(result, dict):
            enqueued = int(result.get("enqueued", len(new_ids)) or 0)
    return {"discovered": len(new_ids), "enqueued": enqueued}


@router.post("/channels/{channel_id}/sync-captions")
def enqueue_sync_captions(
    channel_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(get_paid_user),
) -> Dict[str, Any]:
    """Enqueue captions-only jobs for every video on this channel.

    Unlike /sync-files which is strictly for not-yet-archived videos,
    this endpoint targets ALL of the user's videos on the channel -
    we want to (re)fetch manual captions, which may have been added
    or updated by the creator after the original video sync. The
    worker runs yt-dlp with --skip-download so we don't re-pull the
    mp4; only the .vtt files come down.

    Dedup against pending/running captions jobs so a re-run doesn't
    pile up duplicates. The 'no captions' result is recorded just
    like 'captions found' - both are valid outcomes.
    """
    _require_channel_active(db, current.id, channel_id)
    user_channel = db.get(UserChannel, (current.id, channel_id))
    if user_channel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Channel not found.",
        )

    # Every video on this channel. Skip captions for videos the user
    # doesn't actually have any visibility into (e.g. status=failed
    # before the file was ever synced).
    candidate_rows = (
        db.query(UserChannelVideo)
        .filter(
            UserChannelVideo.user_id == current.id,
            UserChannelVideo.channel_id == channel_id,
        )
        .all()
    )
    candidate_ids = [r.video_id for r in candidate_rows]
    if not candidate_ids:
        return {"enqueued": 0, "skipped_in_flight": 0}

    # Skip videos with an in-flight captions job already pending.
    in_flight = {
        v
        for (v,) in db.query(SyncJob.video_id)
        .filter(
            SyncJob.user_id == current.id,
            SyncJob.channel_id == channel_id,
            SyncJob.video_id.in_(candidate_ids),
            SyncJob.kind == "captions",
            SyncJob.status.in_(["pending", "running"]),
        )
        .all()
    }
    to_enqueue = [v for v in candidate_ids if v not in in_flight]

    # Oldest-upload-first ordering for consistency with /sync-files.
    by_id = {r.video_id: r for r in candidate_rows}

    def _upload_date(video_id: str) -> str:
        row = by_id.get(video_id)
        if row is None:
            return ""
        try:
            return json.loads(row.data_json).get("uploadDate") or ""
        except json.JSONDecodeError:
            return ""

    to_enqueue.sort(key=_upload_date)

    created_ids: List[str] = []
    for vid in to_enqueue:
        job = SyncJob(
            user_id=current.id,
            channel_id=channel_id,
            video_id=vid,
            kind="captions",
        )
        db.add(job)
        db.flush()
        created_ids.append(job.id)
    db.commit()

    return {
        "enqueued": len(created_ids),
        "skipped_in_flight": len(in_flight),
        "job_ids": created_ids,
    }


# ---------- Worker metadata jobs ----------
#
# Private, unlisted and members-only videos get no field upkeep from
# anything the server can reach. The channel-tab enumeration cannot list
# them by definition, and watch-page scraping from our own box comes back
# bot-checked. On the production account that is six of seven archived
# videos - the majority of what we hold, and the whole point of the product.
#
# The desktop worker is the only thing that can see them: it holds the
# user's YouTube cookies in its embedded webview and is signed in as them.
# A "metadata" job asks it to read one video's current fields and hand them
# back. Completion feeds those through the SAME versioned rescan engine the
# OAuth path uses, so there is one diff/snapshot engine in this codebase and
# both sources write history identically.

_METADATA_JOB_KIND = "metadata"

# How many metadata jobs one user may have outstanding (pending or running)
# across every channel at once.
#
# The queue is a single lane - the worker claims one job at a time - and
# metadata is upkeep, while downloading the videos someone is paying us to
# archive is the product. An uncapped sweep of a 500-video channel drops 500
# bookkeeping reads into that lane. 25 is the ceiling: a metadata read is one
# --skip-download info-json fetch, a few seconds, so the whole outstanding
# set is a couple of minutes of worker time and never becomes a queue anyone
# notices. A repeat call tops back up to 25 as jobs drain, so a large channel
# is covered over several passes instead of one pile.
_METADATA_JOBS_MAX_OUTSTANDING = 25

# How long a metadata job waits behind downloads before it stops yielding.
#
# The priority rule in _next_claimable_job would otherwise just invert the
# starvation: someone whose download queue is never empty would get no upkeep
# ever. Letting old metadata jobs rejoin the normal FIFO order is only safe
# because of the cap above - at most _METADATA_JOBS_MAX_OUTSTANDING of them
# exist, so the worst case is those 25 reads going ahead of the downloads
# once a day, not a rebuilt pile.
_METADATA_JOB_STALE_HOURS = 24

# The privacy tiers we are willing to store, matching the frontend's
# VideoPrivacy union. Anything outside this set means the worker told us
# something we cannot map, and an unmappable privacy is a reason to write
# nothing - never a reason to guess.
_VIDEO_PRIVACY_TIERS = ("public", "unlisted", "private", "members")

# Enqueue kill switch. Off unless the deploy explicitly opts in.
#
# The shipped worker binary decides whether to skip the download by
# comparing the job kind to "captions" and nothing else, and /claim mints an
# empty upload url for every non-video kind. So a "metadata" job handed to
# that binary downloads the entire video file and then PUTs it nowhere: full
# bandwidth spent, nothing produced. The capability therefore ships dark.
# This flag is the only thing that turns it on, and deploying the backend
# alone cannot start handing jobs out.
#
# Safe to flip only once every running worker is a build that skips the
# download for this kind and posts the "metadata" object back to
# /sync-jobs/{id}/complete. There is exactly one worker install today (the
# developer's own machine) and the app is not distributed yet, so that is a
# rebuild, not a migration.
_METADATA_JOBS_FLAG = "METADATA_JOBS_ENABLED"


def _metadata_jobs_enabled() -> bool:
    """True only for an explicitly truthy value. Unset, empty or anything
    unrecognised reads as off - a typo in the env must not enable this."""
    return (os.environ.get(_METADATA_JOBS_FLAG) or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


# ---------- Worker comment jobs ----------
#
# Comments are the other field we hold that only the signed-in owner can read
# in full. A private or members-only video hands an anonymous scrape no thread
# at all, and the Data API path (google_oauth.fetch_video_comments) exists only
# for the minority of channels carrying web OAuth - which is why the comments
# cron is inert for worker-authenticated users today. The desktop worker,
# holding the user's cookies, is the one thing that can read them for everyone
# else. A "comments" job asks it to read one video's thread and hand it back,
# and completion feeds the SAME store engine the OAuth cron uses
# (app.comments_rescan), so there is one diff/snapshot engine in this codebase
# and both sources write history identically.
_COMMENTS_JOB_KIND = "comments"

# Metadata and comments are both upkeep and share the worker's single lane.
# The cap matters at least as much here: a comment read is a whole thread, not
# a field dict, so an uncapped sweep of a 500-video channel would drop 500 of
# the heavier reads ahead of the downloads someone is paying us for. Same
# ceiling as metadata, counted across every channel for the same reason, and a
# repeat call tops back up as jobs drain instead of duplicating them.
_COMMENTS_JOBS_MAX_OUTSTANDING = 25

# Enqueue kill switch, off unless the deploy explicitly opts in - the exact
# twin of METADATA_JOBS_ENABLED. The shipped worker binary has no comment path
# at all: handed a "comments" job it compares the kind to "captions", fails
# that test, and downloads the whole mp4 to PUT it nowhere, the same wasted
# bandwidth metadata hit before its own flag existed. So the capability ships
# dark and this flag is the only thing that turns it on. Safe to flip only once
# every running worker is a build that fetches the thread, sets the
# completeness flag honestly, and posts the "comments" object back to
# /sync-jobs/{id}/complete. There is one worker install today (the developer's
# own machine) and the app is not distributed yet, so that is a rebuild, not a
# migration.
_COMMENTS_JOBS_FLAG = "COMMENTS_JOBS_ENABLED"

# Metadata and comments both claim behind real archiving work. Grouping the
# two kinds so the tiering below reads them as one "upkeep" band.
_UPKEEP_JOB_KINDS = (_METADATA_JOB_KIND, _COMMENTS_JOB_KIND)

# Cadence -> days, mirroring scripts/rescan_comments.py. Used only to size the
# deletion debounce (safety guard 4): a comment missing from a single complete
# fetch is not soft-deleted until it was also missing one cadence ago, so one
# truncated read can never strand a "deleted" mark on its own. Comments are the
# heaviest re-pull, so an absent/retired/unknown cadence falls back to the
# slowest option - the same quarterly default the cron uses.
_COMMENTS_CADENCE_DAYS: Dict[str, int] = {
    "weekly": 7,
    "monthly": 28,
    "quarterly": 90,
    "annually": 365,
}
_COMMENTS_DEFAULT_CADENCE = "quarterly"

# Sanity ratio (safety guard 3). Even with the worker's completeness flag set,
# refuse deletions when the fetched thread is implausibly short against yt-dlp's
# reported comment_count - a bot-check or consent interstitial can let yt-dlp
# exit 0 on a truncated subset. reportedTotal counts replies too, so the honest
# comparison is the WHOLE fetched set (top-level plus replies) against it: using
# top-level alone would trip on every video whose replies outnumber its comments
# and suppress deletions forever. This guard only ever turns deletions OFF, so a
# false trip just skips a cleanup pass. The floor keeps it from firing on tiny
# threads where a couple of missing replies would swing the ratio for no reason.
_COMMENTS_SANITY_MIN_REPORTED = 20
_COMMENTS_SANITY_MIN_RATIO = 0.5


def _comments_jobs_enabled() -> bool:
    """True only for an explicitly truthy value. Unset, empty or anything
    unrecognised reads as off - a typo in the env must not enable this."""
    return (os.environ.get(_COMMENTS_JOBS_FLAG) or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _worker_owned_channel(
    db: Session, user_id: str, channel_youtube_id: str
) -> Optional[Channel]:
    """The Channel row when this user holds a live ownership of it, else None.

    Both revocation columns are checked. ``user_revoked_at`` is the point:
    once someone withdraws the worker's access to a channel we stop asking
    the worker to act as that account, and a metadata job is exactly such a
    request - it only works because the worker is signed in as them.
    """
    channel = (
        db.query(Channel)
        .filter(Channel.youtube_id == channel_youtube_id)
        .one_or_none()
    )
    if channel is None:
        return None
    own = (
        db.query(ChannelOwnership.id)
        .filter(
            ChannelOwnership.user_id == user_id,
            ChannelOwnership.channel_id == channel.id,
            ChannelOwnership.revoked_at.is_(None),
            ChannelOwnership.user_revoked_at.is_(None),
        )
        .first()
    )
    return channel if own is not None else None


def enqueue_metadata_jobs(
    db: Session,
    *,
    user_id: str,
    channel_id: str,
    video_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Queue one metadata job per video on an owned channel.

    The guard lives in here rather than in the route so that every caller -
    the route today, the daily upkeep later - goes through the same switch
    and none of them can enqueue behind its back.

    ``video_ids`` restricts the run; None means every video the user has on
    the channel. Videos that already have a metadata job pending or running
    are skipped, so calling this twice cannot produce two jobs for the same
    video - a second call tops the queue up, it does not duplicate it.

    At most _METADATA_JOBS_MAX_OUTSTANDING jobs are left outstanding for the
    user, counted across every channel because the worker drains one shared
    queue. Whatever doesn't fit comes back as ``remaining`` so the caller can
    say so out loud; silently queueing a quarter of a channel and reporting
    success would promise a refresh that isn't coming.

    Returns counters plus the two reasons nothing may have happened:
    ``owned`` False (we will not ask the worker to act as an account the
    user has not authenticated, or has revoked) and ``enabled`` False (the
    switch above). Callers must surface those rather than reporting a queue
    length of zero as success.
    """
    _require_channel_active(db, user_id, channel_id)
    result: Dict[str, Any] = {
        "owned": True,
        "enabled": True,
        "enqueued": 0,
        "skipped_in_flight": 0,
        "remaining": 0,
        "job_ids": [],
    }

    # Tracking, not ownership - the same change the comment twin got, and
    # for the same reason. Per-video metadata (title, description, tags,
    # view count, duration, thumbnail) is public data; this very file
    # already fetches all of it credential-free in
    # _apply_video_metadata_toggles. Requiring ownership meant a
    # worker-only user could never refresh metadata on their own public
    # catalogue, while the harder capability - comments - was open to them.
    tracked = (
        db.query(UserChannel.user_id)
        .filter(
            UserChannel.user_id == user_id,
            UserChannel.channel_id == channel_id,
            UserChannel.removed_at.is_(None),
        )
        .first()
    )
    if tracked is None:
        result["owned"] = False
        result["enabled"] = _metadata_jobs_enabled()
        return result
    if not _metadata_jobs_enabled():
        result["enabled"] = False
        return result

    query = db.query(UserChannelVideo).filter(
        UserChannelVideo.user_id == user_id,
        UserChannelVideo.channel_id == channel_id,
    )
    if video_ids is not None:
        if not video_ids:
            return result
        query = query.filter(UserChannelVideo.video_id.in_(video_ids))
    rows = query.all()
    if not rows:
        return result

    candidate_ids = [r.video_id for r in rows]
    # Idempotency: a video that already has a metadata job waiting or in
    # progress is not queued again. Two clicks on refresh give one job.
    in_flight = {
        v
        for (v,) in db.query(SyncJob.video_id)
        .filter(
            SyncJob.user_id == user_id,
            SyncJob.channel_id == channel_id,
            SyncJob.video_id.in_(candidate_ids),
            SyncJob.kind == _METADATA_JOB_KIND,
            SyncJob.status.in_(["pending", "running"]),
        )
        .all()
    }

    def _upload_date(row: UserChannelVideo) -> str:
        try:
            return json.loads(row.data_json).get("uploadDate") or ""
        except json.JSONDecodeError:
            return ""

    to_enqueue = sorted(
        (r for r in rows if r.video_id not in in_flight), key=_upload_date
    )

    # The cap counts the user's whole outstanding set, not just this
    # channel's. Per-channel headroom would let someone with six channels
    # queue six caps' worth into the one lane the worker drains.
    outstanding = (
        db.query(SyncJob.id)
        .filter(
            SyncJob.user_id == user_id,
            SyncJob.kind == _METADATA_JOB_KIND,
            SyncJob.status.in_(["pending", "running"]),
        )
        .count()
    )
    headroom = max(0, _METADATA_JOBS_MAX_OUTSTANDING - outstanding)
    result["remaining"] = max(0, len(to_enqueue) - headroom)
    to_enqueue = to_enqueue[:headroom]

    created_ids: List[str] = []
    for row in to_enqueue:
        job = SyncJob(
            user_id=user_id,
            channel_id=channel_id,
            video_id=row.video_id,
            kind=_METADATA_JOB_KIND,
        )
        db.add(job)
        db.flush()
        created_ids.append(job.id)
    db.commit()

    result["enqueued"] = len(created_ids)
    result["skipped_in_flight"] = len(in_flight)
    result["job_ids"] = created_ids
    return result


def enqueue_comment_jobs(
    db: Session,
    *,
    user_id: str,
    channel_id: str,
    video_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Queue one comment job per video on an owned channel.

    The comment twin of enqueue_metadata_jobs, and deliberately identical in
    shape so the two upkeep sweeps behave the same way. The guard lives in
    here rather than the caller so nothing can enqueue behind the switch's
    back. ``video_ids`` restricts the run; None means every video the user has
    on the channel. A video that already has a comment job pending or running
    is skipped, so calling this twice tops the queue up rather than doubling
    it. At most _COMMENTS_JOBS_MAX_OUTSTANDING jobs are left outstanding for
    the user, counted across every channel because the worker drains one shared
    queue; whatever doesn't fit comes back as ``remaining`` so the caller can
    say so instead of reporting a partial sweep as done.

    Returns counters plus the two reasons nothing may have happened: ``owned``
    False (we will not ask the worker to act as an account the user has not
    authenticated, or has revoked) and ``enabled`` False (COMMENTS_JOBS_ENABLED
    is off). Callers must surface those rather than reading a queue length of
    zero as success.
    """
    _require_channel_active(db, user_id, channel_id)
    result: Dict[str, Any] = {
        "owned": True,
        "enabled": True,
        "enqueued": 0,
        "skipped_in_flight": 0,
        "remaining": 0,
        "job_ids": [],
    }

    # Tracking, not ownership. Comments on public videos need no
    # credentials; requiring ownership here meant a user who never signed
    # in to Google could not sync comments on their own public catalogue.
    # Ownership still decides how MUCH is reachable - the worker only sees
    # private and unlisted comments when it is signed in as the owner -
    # but that is coverage, not permission.
    tracked = (
        db.query(UserChannel.user_id)
        .filter(
            UserChannel.user_id == user_id,
            UserChannel.channel_id == channel_id,
            UserChannel.removed_at.is_(None),
        )
        .first()
    )
    if tracked is None:
        result["owned"] = False
        result["enabled"] = _comments_jobs_enabled()
        return result
    if not _comments_jobs_enabled():
        result["enabled"] = False
        return result

    query = db.query(UserChannelVideo).filter(
        UserChannelVideo.user_id == user_id,
        UserChannelVideo.channel_id == channel_id,
    )
    if video_ids is not None:
        if not video_ids:
            return result
        query = query.filter(UserChannelVideo.video_id.in_(video_ids))
    rows = query.all()
    if not rows:
        return result

    candidate_ids = [r.video_id for r in rows]
    # Idempotency: a video that already has a comment job waiting or in
    # progress is not queued again. Two clicks give one job.
    in_flight = {
        v
        for (v,) in db.query(SyncJob.video_id)
        .filter(
            SyncJob.user_id == user_id,
            SyncJob.channel_id == channel_id,
            SyncJob.video_id.in_(candidate_ids),
            SyncJob.kind == _COMMENTS_JOB_KIND,
            SyncJob.status.in_(["pending", "running"]),
        )
        .all()
    }

    def _upload_date(row: UserChannelVideo) -> str:
        try:
            return json.loads(row.data_json).get("uploadDate") or ""
        except json.JSONDecodeError:
            return ""

    to_enqueue = sorted(
        (r for r in rows if r.video_id not in in_flight), key=_upload_date
    )

    # The cap counts the user's whole outstanding set, not just this channel's,
    # for the same reason metadata does: one shared lane, and per-channel
    # headroom would let a six-channel user queue six caps' worth into it.
    outstanding = (
        db.query(SyncJob.id)
        .filter(
            SyncJob.user_id == user_id,
            SyncJob.kind == _COMMENTS_JOB_KIND,
            SyncJob.status.in_(["pending", "running"]),
        )
        .count()
    )
    headroom = max(0, _COMMENTS_JOBS_MAX_OUTSTANDING - outstanding)
    result["remaining"] = max(0, len(to_enqueue) - headroom)
    to_enqueue = to_enqueue[:headroom]

    created_ids: List[str] = []
    for row in to_enqueue:
        job = SyncJob(
            user_id=user_id,
            channel_id=channel_id,
            video_id=row.video_id,
            kind=_COMMENTS_JOB_KIND,
        )
        db.add(job)
        db.flush()
        created_ids.append(job.id)
    db.commit()

    result["enqueued"] = len(created_ids)
    result["skipped_in_flight"] = len(in_flight)
    result["job_ids"] = created_ids
    return result


@router.post("/channels/{channel_id}/sync-metadata-jobs")
def enqueue_sync_metadata_jobs(
    channel_id: str,
    payload: Optional[Dict[str, Any]] = Body(default=None),
    db: Session = Depends(get_db),
    current: User = Depends(get_paid_user),
) -> Dict[str, Any]:
    """Ask the user's own worker to re-read metadata for videos on a channel.

    Body: ``{"video_ids": [...]}`` (optional). Omit it to cover every video
    the user has on the channel.

    This is the counterpart to /sync-metadata, which refreshes through the
    Data API and therefore only exists for the minority of channels that
    have web OAuth behind them. This path works for the rest, and it is the
    only path that can see sealed videos at all.
    """
    video_ids: Optional[List[str]] = None
    if isinstance(payload, dict) and payload.get("video_ids") is not None:
        raw = payload.get("video_ids")
        if not isinstance(raw, list):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="video_ids must be a list.",
            )
        video_ids = [v for v in raw if isinstance(v, str) and v]

    outcome = enqueue_metadata_jobs(
        db, user_id=current.id, channel_id=channel_id, video_ids=video_ids
    )
    if not outcome["owned"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You haven't authenticated ownership of this channel.",
        )
    if not outcome["enabled"]:
        # Deliberately not a quiet no-op. Reporting "enqueued 0" would read
        # as "nothing needed doing" and the user would wait for a refresh
        # that is never coming.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Metadata jobs aren't enabled on this deployment yet.",
        )
    # ``remaining`` is how many videos were left out because the user is at
    # their outstanding-jobs cap. It is reported rather than hidden: the
    # caller has to be able to tell "the channel is refreshed" from "the
    # first 25 are queued, call again when they drain".
    return {
        "enqueued": outcome["enqueued"],
        "skipped_in_flight": outcome["skipped_in_flight"],
        "remaining": outcome["remaining"],
        "job_ids": outcome["job_ids"],
    }


@router.get("/sync-jobs/active")
def list_active_sync_jobs(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    """Return the user's pending/running sync jobs.

    Frontend polls this every couple seconds while any video is in the
    ``syncing`` state, then matches by video_id to update progress bars.

    ``kind`` is reported so the caller can tell a file download from a
    captions or metadata refresh. Matching on video_id alone would put a
    fully-archived video's card into "syncing" because we queued a metadata
    read for it, which tells the user a download is happening when none is.
    """
    rows = (
        db.query(SyncJob)
        .filter(
            SyncJob.user_id == current.id,
            SyncJob.status.in_(["pending", "running"]),
        )
        .order_by(SyncJob.created_at)
        .all()
    )
    return [
        {
            "id": r.id,
            "channelId": r.channel_id,
            "videoId": r.video_id,
            "kind": r.kind,
            "status": r.status,
            "progress": r.progress,
            "createdAt": r.created_at.isoformat() if r.created_at else None,
            "startedAt": r.started_at.isoformat() if r.started_at else None,
        }
        for r in rows
    ]


# ---------- Worker client endpoints ----------
#
# A "worker client" is the user's local machine running the ARCHIVE336 desktop
# app. These endpoints let it
# claim sync jobs, heartbeat while working, and report results. The
# client uses cookie auth like the web frontend — same session.

# A claim is considered stale if its last heartbeat is older than this.
# When a stale claim is detected, the job is reverted to 'pending' so
# another client can pick it up.
HEARTBEAT_STALE_SECONDS = 300

# Why a pending job was cancelled. Distinguishable from a real download
# failure so the give-up counter never blames a video for work the user
# called off - re-adding the channel must not find it pre-condemned.
_CANCELLED_CHANNEL_REMOVED = "cancelled: channel removed"


def _reap_stale_claims(db: Session, user_id: str) -> None:
    """Revert THIS user's stale claims back to 'pending'.

    Scoped to the caller, and that matters. Unscoped, one user's routine
    poll reaped another user's actively-running download the moment it
    passed HEARTBEAT_STALE_SECONDS. The original worker then PUT its mp4
    to B2 on a still-valid presigned URL and got a 409 from /complete, and
    the worker deliberately does not fail a job on 409 - so the object
    landed in the bucket with no storage_ledger row. Bytes we pay for and
    cannot see. A claim is only ever stale from the point of view of the
    machine that made it.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=HEARTBEAT_STALE_SECONDS
    )
    stale = (
        db.query(SyncJob)
        .filter(
            SyncJob.user_id == user_id,
            SyncJob.status == "running",
            SyncJob.heartbeat_at.isnot(None),
            SyncJob.heartbeat_at < cutoff,
        )
        .all()
    )
    for job in stale:
        job.status = "pending"
        job.claimed_by = None
        job.heartbeat_at = None
    if stale:
        db.commit()


def _paused_channel_ids(db: Session, user_id: str) -> Set[str]:
    """YouTube ids of channels this user has switched off.

    Read from the subscription row, which is where every other read is
    served from, so a stale legacy copy cannot resurrect a paused
    channel.
    """
    paused: Set[str] = set()
    rows = (
        db.query(UserChannelSubscription.settings_json, Channel.youtube_id)
        .join(Channel, Channel.id == UserChannelSubscription.channel_id)
        .filter(
            UserChannelSubscription.user_id == user_id,
            UserChannelSubscription.unsubscribed_at.is_(None),
        )
    )
    for settings_json, youtube_id in rows:
        if not settings_json:
            continue
        try:
            stored = json.loads(settings_json)
        except (TypeError, ValueError):
            continue
        if isinstance(stored, dict) and stored.get("active", True) is False:
            paused.add(youtube_id)
    return paused


def _next_claimable_job(db: Session, user_id: str) -> Optional[SyncJob]:
    """The pending job this user's worker should take next.

    Archiving work outranks upkeep. A pending video (or captions) job is
    handed out ahead of any pending metadata OR comment job however old the
    upkeep job is, because both are bookkeeping on something we already hold
    and a download is the thing the user is paying for. A comment sweep must
    never jump ahead of the videos being downloaded, so it sits in the same
    demoted band as metadata. Ordering is still FIFO inside each tier.

    Only the upkeep kinds (_UPKEEP_JOB_KINDS: metadata + comments) are
    demoted. video/captions/NULL keep the single FIFO order they shipped with
    - captions are archive content too, and re-ranking them was not the
    problem here.

    The exception keeps this from being the same bug pointing the other way:
    once the oldest upkeep job has waited _METADATA_JOB_STALE_HOURS it rejoins
    the normal order, so a user whose download queue is never empty still gets
    upkeep. Ages are compared in Python rather than SQL to stay off the
    naive/aware timestamp mismatch the reaper above also sidesteps.
    """
    pending = db.query(SyncJob).filter(
        SyncJob.user_id == user_id,
        SyncJob.status == "pending",
    )
    # A paused channel hands out no work, including work queued before it
    # was paused. Gating only the enqueue endpoints left the owner with
    # 405 jobs already in the queue that kept downloading after he
    # switched the channel off - the pause looked broken because, for
    # everything already queued, it was.
    #
    # Skipped rather than cancelled, deliberately. The jobs stay pending,
    # so switching the channel back on resumes exactly where it stopped
    # instead of requiring the whole catalogue to be re-queued.
    paused = _paused_channel_ids(db, user_id)
    if paused:
        pending = pending.filter(SyncJob.channel_id.notin_(paused))
    work = (
        pending.filter(
            or_(SyncJob.kind.notin_(_UPKEEP_JOB_KINDS), SyncJob.kind.is_(None))
        )
        .order_by(SyncJob.created_at)
        .first()
    )
    upkeep = (
        pending.filter(SyncJob.kind.in_(_UPKEEP_JOB_KINDS))
        .order_by(SyncJob.created_at)
        .first()
    )
    if upkeep is None:
        return work
    if work is None:
        return upkeep

    created = upkeep.created_at
    if created is not None:
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        starved_before = datetime.now(timezone.utc) - timedelta(
            hours=_METADATA_JOB_STALE_HOURS
        )
        if created < starved_before:
            return upkeep
    return work


@router.get("/sync-jobs/claim")
def claim_sync_job(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> Optional[Dict[str, Any]]:
    """Atomically claim the next pending sync job for this user.

    "Next" is oldest-first within a tier, with archiving work ahead of
    metadata/comment upkeep - _next_claimable_job owns that choice.

    Returns ``None`` (HTTP 200 with body 'null') when there's nothing to do —
    the client just polls again later. When a job is claimed the response
    includes a presigned R2 PUT URL the client uploads the .mp4 to.

    Side effect: bumps last_seen_at on the calling session. The worker
    app polls this endpoint every few seconds while running, so it
    doubles as a worker heartbeat - the worker-status endpoint reads
    last_seen_at to tell the website's UI whether to show 'worker app
    inactive' or normal sync progress.
    """
    _reap_stale_claims(db, current.id)

    # No NEW work for a paused account.
    #
    # Deliberately not get_paid_user on the route: security.py keeps the
    # worker endpoints open on purpose so a job already in flight runs to
    # completion and its bytes get recorded. This stops the next job, not
    # the current one - heartbeat, complete and fail all stay open. Without
    # it a lapsed card kept a queued back catalogue downloading for as long
    # as it took to drain, billing storage the whole way.
    if not service_is_active(current):
        return None

    # Heartbeat: bump last_seen_at on the calling session (cheap write,
    # only when at least 5s have elapsed since the last bump to avoid
    # hammering the DB on every poll). Best-effort - failure here must
    # not block job claim.
    if session_token:
        try:
            s = db.get(UserSession, session_token)
            if s is not None:
                now_ts = datetime.now(timezone.utc)
                last = s.last_seen_at
                if last is not None and last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if last is None or (now_ts - last).total_seconds() > 5:
                    s.last_seen_at = now_ts
                    db.commit()
        except Exception:
            log.exception("failed to bump session last_seen_at")
            db.rollback()

    # Downloads first, then upkeep. See _next_claimable_job.
    job = _next_claimable_job(db, current.id)
    if job is None:
        return None

    # Atomically transition pending → running. If someone else (another
    # client tab, the server worker) beat us to it, the rowcount is 0 and
    # we treat as "no job for me right now".
    now = datetime.now(timezone.utc)
    rowcount = (
        db.query(SyncJob)
        .filter(SyncJob.id == job.id, SyncJob.status == "pending")
        .update(
            {
                "status": "running",
                "claimed_by": current.id,
                "started_at": now,
                "heartbeat_at": now,
                "progress": 0.0,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    if rowcount == 0:
        return None

    db.refresh(job)
    # Only mint an mp4 presigned PUT URL for video-kind jobs. Captions-
    # kind jobs don't touch the mp4 - they upload individual VTT files
    # via the per-language /caption-upload-url endpoint below.
    thumb_key = ""
    thumb_upload_url = ""
    if job.kind == "video":
        r2_key = r2_paths.video_key(job.user_id, job.video_id)
        upload_url = r2.presign_put(
            # 6h, not 1h. A 4 GB archive on a residential uplink takes
            # longer than an hour to PUT, so the URL expired mid-upload,
            # the job failed, and every retry failed identically forever -
            # the bigger the video, the more certain it could never be
            # backed up.
            r2_key, expires_in=21600, content_type="video/mp4", subject=current.id,
        )
        # Thumbnails can't be back-filled from YouTube's CDN for private
        # videos (i.ytimg.com 404s those), so the authenticated worker is
        # the only thing that can capture them. Hand it a presigned slot
        # alongside the video upload.
        thumb_key = r2_paths.thumb_key(job.user_id, job.video_id)
        thumb_upload_url = r2.presign_put(
            thumb_key, expires_in=21600, content_type="image/jpeg",
            subject=current.id,
        )
    else:
        r2_key = ""
        upload_url = ""

    # Ship the channel's quality + captions settings with the job so the
    # worker actually downloads what the user asked for. Without these the
    # downloader hardcoded 1080p/H.264 and always pulled captions, making
    # those settings cosmetic.
    ch_row = db.get(UserChannel, (job.user_id, job.channel_id))
    job_settings: Dict[str, Any] = {}
    if ch_row is not None:
        try:
            job_settings = (json.loads(ch_row.data_json) or {}).get(
                "settings"
            ) or {}
        except (json.JSONDecodeError, TypeError):
            job_settings = {}

    return {
        "id": job.id,
        "channelId": job.channel_id,
        "videoId": job.video_id,
        "kind": job.kind,
        "youtubeUrl": f"https://www.youtube.com/watch?v={job.video_id}",
        "r2Key": r2_key,
        "uploadUrl": upload_url,
        "uploadContentType": "video/mp4",
        "thumbnailUploadUrl": thumb_upload_url,
        "thumbnailR2Key": thumb_key,
        "maxResolution": job_settings.get("maxResolution") or "1080p",
        "codecPreference": job_settings.get("codecPreference") or "compat",
        "saveCaptions": bool(job_settings.get("saveCaptions", True)),
    }


def _preserve_caption_history(
    db: Session,
    *,
    user_id: str,
    channel_id: str,
    video_id: str,
    language: str,
    new_sha: Optional[str],
) -> None:
    """Before the worker overwrites a caption's canonical .vtt, preserve the
    prior version as history — but only when the transcript actually changed
    (new_sha differs from the last one we recorded) and the channel has
    caption history enabled.

    Best-effort and fully self-contained (own commit): any failure is logged
    and swallowed so caption capture is never blocked. Needs the worker to
    send the file's sha256; older workers omit it, in which case we simply
    can't detect changes and skip history.
    """
    if not new_sha:
        return
    try:
        ch = db.get(UserChannel, (user_id, channel_id))
        if ch is None:
            return
        try:
            ch_settings = (json.loads(ch.data_json) or {}).get("settings") or {}
        except (json.JSONDecodeError, TypeError):
            ch_settings = {}
        if not ch_settings.get("saveCaptionsHistory", True):
            return

        video = (
            db.query(UserChannelVideo)
            .filter(
                UserChannelVideo.user_id == user_id,
                UserChannelVideo.channel_id == channel_id,
                UserChannelVideo.video_id == video_id,
            )
            .one_or_none()
        )
        if video is None:
            return
        try:
            data = json.loads(video.data_json)
        except (json.JSONDecodeError, TypeError):
            return

        versions = data.get("captionVersions")
        if not isinstance(versions, dict):
            versions = {}
        prev = versions.get(language)
        prev = prev if isinstance(prev, dict) else None
        prev_sha = prev.get("sha") if prev else None

        now = datetime.now(timezone.utc)

        # Snapshot the prior version only when there WAS one and it changed.
        if prev_sha and prev_sha != new_sha:
            canonical = r2_paths.caption_key(user_id, video_id, language)
            head = None
            try:
                head = r2.head(canonical, subject=user_id)
            except Exception:  # noqa: BLE001
                head = None
            client = r2.client()
            bucket = r2.bucket()
            if head is not None and client and bucket:
                ts = now.strftime("%Y%m%dT%H%M%SZ")
                hist_key = r2_paths.caption_history_key(
                    user_id, video_id, language, ts
                )
                try:
                    client.copy_object(
                        Bucket=bucket,
                        Key=hist_key,
                        CopySource={"Bucket": bucket, "Key": canonical},
                    )
                except Exception:  # noqa: BLE001
                    log.exception(
                        "caption history copy failed for %s/%s/%s",
                        user_id, video_id, language,
                    )
                else:
                    storage_ledger.record_object(
                        db,
                        user_id=user_id,
                        r2_key=hist_key,
                        byte_count=int(head.get("ContentLength") or 0),
                        kind="snapshot",
                        uploaded_at=now,
                        metadata_bytes=r2.metadata_bytes_for(
                            content_type=head.get("ContentType"),
                            custom_meta=head.get("Metadata"),
                        ),
                    )
                    captured_at = now
                    prev_at = prev.get("at") if prev else None
                    if isinstance(prev_at, str):
                        try:
                            captured_at = datetime.fromisoformat(prev_at)
                        except ValueError:
                            captured_at = now
                    db.add(
                        VideoFieldSnapshot(
                            user_id=user_id,
                            channel_id=channel_id,
                            video_id=video_id,
                            field="captions",
                            value_json=json.dumps(
                                {"language": language, "sha256": prev_sha}
                            ),
                            r2_key=hist_key,
                            captured_at=captured_at,
                            last_seen_at=captured_at,
                            superseded_at=now,
                        )
                    )

        # Record the new current version (used to detect the next change).
        versions[language] = {"sha": new_sha, "at": now.isoformat()}
        data["captionVersions"] = versions
        video.data_json = json.dumps(data)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception(
            "caption history preservation failed for %s/%s/%s",
            user_id, video_id, language,
        )


@router.post("/sync-jobs/{job_id}/caption-upload-url")
def caption_upload_url(
    job_id: str,
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Mint a presigned R2 PUT URL for a single caption (.vtt) file.

    The worker calls this once per language it finds via yt-dlp, then
    PUTs the VTT bytes directly to R2 at
    ``videos/{video_id}/captions/{language}.vtt``.

    Language code is sanitized to a conservative ``[A-Za-z0-9_-]``
    allowlist - yt-dlp's BCP47-ish codes (en, en-US, pt-BR, zh-Hant)
    all stay intact, anything weird gets refused.
    """
    job = db.get(SyncJob, job_id)
    if job is None or job.user_id != current.id:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "running":
        raise HTTPException(
            status_code=409,
            detail=f"Job is {job.status}, not running",
        )
    language = (payload or {}).get("language")
    if not isinstance(language, str) or not language:
        raise HTTPException(status_code=400, detail="language is required")
    # Defense-in-depth: BCP47 codes are letters/digits/dash only.
    safe = "".join(
        ch for ch in language if ch.isalnum() or ch in ("-", "_")
    )
    if not safe or safe != language or len(safe) > 32:
        raise HTTPException(status_code=400, detail="invalid language code")

    # Preserve the prior caption version as history before the worker
    # overwrites the canonical key (no-op unless it actually changed +
    # caption history is enabled + the worker sent a sha256).
    raw_sha = (payload or {}).get("sha256")
    _preserve_caption_history(
        db,
        user_id=job.user_id,
        channel_id=job.channel_id,
        video_id=job.video_id,
        language=safe,
        new_sha=raw_sha if isinstance(raw_sha, str) and raw_sha else None,
    )

    key = r2_paths.caption_key(job.user_id, job.video_id, safe)
    url = r2.presign_put(key, expires_in=3600, content_type="text/vtt", subject=current.id)
    return {
        "uploadUrl": url,
        "uploadContentType": "text/vtt",
        "r2Key": key,
    }


# Worker is treated as active if any of its sessions polled within
# this window. The worker polls /sync-jobs/claim every 3s so even
# accounting for slow networks 30s is generous.
_WORKER_ACTIVE_WINDOW = timedelta(seconds=30)


@router.get("/worker-status")
def worker_status(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return whether a worker app is currently active for this user.

    'Active' means any UserSession with a worker User-Agent has bumped
    its last_seen_at within the last 30s. The ChannelDetail page polls
    this whenever there's a video in 'syncing' status, so it can
    distinguish "worker is grinding through this" from "job is queued
    and there's no worker to pick it up".
    """
    cutoff = datetime.now(timezone.utc) - _WORKER_ACTIVE_WINDOW
    row = (
        db.query(UserSession)
        .filter(
            UserSession.user_id == current.id,
            UserSession.user_agent.isnot(None),
            UserSession.user_agent.like("ARCHIVE336-Archive-Tool-Desktop/%"),
        )
        .order_by(UserSession.last_seen_at.desc().nullslast())
        .first()
    )
    last_seen = row.last_seen_at if row else None
    if last_seen is not None and last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    active = last_seen is not None and last_seen > cutoff
    # Liveness is not health. A worker can check in every 30 seconds while
    # every download fails, or while its YouTube sign-in has lapsed so it
    # only ever gets public videos - and the dashboard would have happily
    # said "running" throughout. The owner's ask is to "feel safe that my
    # channels are being backed up", and only the fields below can honestly
    # support that claim, so they are computed here rather than letting the
    # UI infer wellness from a heartbeat.
    #
    # All of this reads data that already flows. The worker reports its
    # YouTube connection on every launch (PUT /worker-connection), and job
    # outcomes are already recorded - so this needs no new worker build and
    # no new table, which also means it cannot itself go stale separately.
    conn = db.get(WorkerYoutubeConnection, current.id)
    # A stale report is not a healthy one. If the worker has not spoken
    # since before its current run, we know nothing current about its
    # sign-in, so we decline to vouch for it rather than showing a green
    # state built on a week-old claim.
    youtube_ok = bool(conn and conn.connected)

    # Videos that are genuinely not backed up.
    #
    # This used to count failed SyncJob ROWS from the last 24 hours, which
    # is a different thing in three ways at once: it counted every attempt
    # rather than every video, it counted metadata and comment jobs as
    # "videos", and it never asked whether the video succeeded afterwards.
    # So the night the storage bucket filled, the owner's dashboard read
    # "24 videos failed to back up" directly above "ARCHIVED 11 / 11" -
    # every video safely stored, and an alarm about them anyway. An alarm
    # that fires when nothing is wrong is worse than no alarm, because it
    # is the one you learn to scroll past.
    #
    # A video counts only if there is no file, nothing queued to make one,
    # and at least one failed attempt behind it. No time window: a video
    # stuck since last week is still not backed up, and ageing the alarm
    # out would be a way of forgetting rather than fixing.
    failed_ids = {
        v
        for (v,) in db.query(SyncJob.video_id)
        .filter(
            SyncJob.user_id == current.id,
            SyncJob.kind == "video",
            SyncJob.status == "failed",
        )
        .distinct()
    }
    if failed_ids:
        queued = {
            v
            for (v,) in db.query(SyncJob.video_id)
            .filter(
                SyncJob.user_id == current.id,
                SyncJob.kind == "video",
                SyncJob.status.in_(("pending", "running")),
                SyncJob.video_id.in_(failed_ids),
            )
            .distinct()
        }
        stored = set()
        for row in db.query(UserChannelVideo).filter(
            UserChannelVideo.user_id == current.id,
            UserChannelVideo.video_id.in_(failed_ids),
        ):
            try:
                if (json.loads(row.data_json) or {}).get("status") == "archived":
                    stored.add(row.video_id)
            except (json.JSONDecodeError, TypeError):
                continue
        failed_recently = len(failed_ids - queued - stored)
    else:
        failed_recently = 0
    pending = (
        db.query(SyncJob)
        .filter(
            SyncJob.user_id == current.id,
            SyncJob.status.in_(("pending", "running")),
        )
        .count()
    )
    tracked_channels = (
        db.query(UserChannel)
        .filter(
            UserChannel.user_id == current.id,
            UserChannel.removed_at.is_(None),
        )
        .count()
    )

    return {
        "active": active,
        "lastSeenAt": last_seen.isoformat() if last_seen else None,
        # Paused accounts genuinely are not being backed up any more, and
        # the card must say so. Without this it would reach its all-clear
        # state - alive, signed in, no failures, no queue - and claim the
        # channels are backed up, which after a failed card is exactly
        # backwards. Pausing quietly would be worse than not pausing.
        "billingPaused": not service_is_active(current),
        "youtubeAuthOk": youtube_ok,
        "youtubeReportedAt": (
            conn.reported_at.isoformat() if conn and conn.reported_at else None
        ),
        "failedJobs": failed_recently,
        "pendingJobs": pending,
        "trackedChannels": tracked_channels,
    }


@router.post("/sync-jobs/{job_id}/heartbeat", status_code=status.HTTP_204_NO_CONTENT)
def heartbeat_sync_job(
    job_id: str,
    payload: Optional[Dict[str, Any]] = None,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> Response:
    """Refresh the claim's heartbeat. Optionally update progress (0..1).

    Also bumps the calling session's last_seen_at so the website's
    worker-status endpoint stays "active" while the worker is busy
    on a single long-running download (the worker only hits /claim
    between jobs, so without this the status flips to inactive after
    30s of pure heartbeats).
    """
    job = db.get(SyncJob, job_id)
    if job is None or job.user_id != current.id:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.claimed_by != current.id or job.status != "running":
        raise HTTPException(status_code=409, detail="Job not claimed by you.")
    job.heartbeat_at = datetime.now(timezone.utc)

    # Bump session.last_seen_at the same way /claim does so the
    # worker-status endpoint reads as active throughout a download.
    # Throttle to one write per 5s per session to keep the per-tick
    # heartbeat traffic cheap.
    if session_token:
        try:
            s = db.get(UserSession, session_token)
            if s is not None:
                now_ts = datetime.now(timezone.utc)
                last = s.last_seen_at
                if last is not None and last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if last is None or (now_ts - last).total_seconds() > 5:
                    s.last_seen_at = now_ts
        except Exception:
            log.exception("failed to bump session last_seen_at on heartbeat")
    if payload and isinstance(payload.get("progress"), (int, float)):
        job.progress = max(0.0, min(float(payload["progress"]), 0.99))

        # Mirror progress onto the linked video row so the UI's poller
        # picks it up without an extra query - but only for the job kind
        # that is actually fetching the file, the same line fail_sync_job
        # draws. ``status`` on the video row means "state of this video's
        # archive": a captions or metadata job is upkeep on something we
        # already hold, so writing "syncing" over an archived row claims a
        # download is running that nobody asked for. And it sticks - only
        # the video-kind completion writes "archived" back, so a row put
        # into "syncing" by an upkeep job stays there for good.
        if job.kind in (None, "video"):
            video = (
                db.query(UserChannelVideo)
                .filter_by(
                    user_id=job.user_id,
                    channel_id=job.channel_id,
                    video_id=job.video_id,
                )
                .first()
            )
            if video:
                try:
                    data = json.loads(video.data_json)
                except json.JSONDecodeError:
                    data = {}
                data["status"] = "syncing"
                data["syncProgress"] = job.progress
                video.data_json = json.dumps(data)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _privacy_from_availability(availability: Optional[str]) -> Optional[str]:
    """Map yt-dlp's ``availability`` (the worker reads it off the video's
    info-json) to our privacy tier. This is what lets a public video land
    Open and a private/members one Sealed, instead of the discovery-time
    "private" placeholder sealing everything. Returns None when we can't
    tell (unknown/missing) so the caller keeps the existing safe value
    rather than guessing.

    Members-only is spelled "members" here, matching the watch-page
    classifier and the frontend's VideoPrivacy union. The backend used to
    write "members_only" from this one mapper, which the frontend has never
    understood - it is not a second tier, it was a typo with a long life.
    """
    if not availability or not isinstance(availability, str):
        return None
    return {
        "public": "public",
        "unlisted": "unlisted",
        "private": "private",
        "premium_only": "members",
        "subscriber_only": "members",
        "needs_auth": "private",
    }.get(availability.strip().lower())


# yt-dlp availability values that are not a reading of the video's privacy.
#
# "needs_auth" means yt-dlp could not get in. From a worker signed in as the
# channel owner that is a lapsed cookie, not a creator privating their video,
# and _privacy_from_availability answers "private" for it - which is the
# right guess at download time (it seals rather than exposes) but a lie in
# version history, where it reads as "the creator privated this" on the day
# the worker's session expired. A failed look writes nothing, so this is
# rejected here and the whole payload goes with it.
_WORKER_PRIVACY_NOT_A_READING = ("needs_auth",)


def _normalize_worker_privacy(raw: Any) -> Optional[str]:
    """One of our four privacy tiers, or None when we cannot tell.

    Accepts either a tier we already speak or one of yt-dlp's
    ``availability`` values, since the worker reads that off the info-json.
    None means "unrecognised", and the caller writes nothing at all on None:
    privacy is a versioned field, so guessing it would both publish a false
    history entry and change what the archive says a video is.
    """
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    if not value:
        return None
    if value in _WORKER_PRIVACY_NOT_A_READING:
        return None
    if value in _VIDEO_PRIVACY_TIERS:
        return value
    return _privacy_from_availability(value)


# The three tiers every source in this archive spells the same way.
#
# There are two sources for privacy and they answer different questions:
#
#   Data API  status.privacyStatus -> public | unlisted | private
#   worker    yt-dlp availability  -> public | unlisted | private |
#                                     premium_only, subscriber_only -> members
#
# Member-gating is a second axis in reality - a video is listed or not, and
# separately paywalled or not - and both sources flatten it into this one
# field. The Data API cannot see the gate at all: a members-only video comes
# back with privacyStatus "public". So a stored "public" that the OAuth
# rescan wrote, set against a worker reading of "members", is not two answers
# to one question. It is one answer each to two different questions, and
# versioning the difference publishes "the creator made this members-only" on
# a day the creator did nothing - then flips it back on the next OAuth
# refresh, one false history entry per pass, forever.
#
# So a privacy difference is only attributed to the creator when both sides
# sit inside this shared vocabulary. Anything crossing into or out of
# "members" leaves the stored value alone and writes no snapshot. That does
# lose a genuine members flip, as a gap. A gap is something missing; a false
# entry is a lie about what the user did, and this archive trades the first
# for the second every time.
_SHARED_PRIVACY_VOCABULARY = ("public", "unlisted", "private")


def _fill_absent_privacy(
    row: UserChannelVideo, stored: Dict[str, Any], observed: str
) -> bool:
    """Write privacy onto a row that holds none, without history.

    Same reasoning as _fill_absent_flat_fields: with no previous value there
    is nothing to supersede, and routing the first one through the versioning
    engine would render as a change from nothing that the creator never made.
    Every row is stamped with a privacy at discovery, so this is the legacy or
    damaged case rather than the usual one.

    Mutates ``stored`` as well as the row so the caller's copy matches what
    the engine reads back. Caller owns the commit.
    """
    current = stored.get("privacy")
    if isinstance(current, str) and current.strip().lower() in _VIDEO_PRIVACY_TIERS:
        return False
    # Re-read the row instead of re-serialising the caller's copy.
    # _record_sighting has already written to this row by the time we get
    # here, and dumping a dict that was parsed before it ran would put the
    # removal strikes it just cleared straight back - including flipping a
    # row it had restored from "deleted_on_youtube" back to deleted, right
    # after the worker proved the video is still there.
    try:
        data = json.loads(row.data_json)
    except json.JSONDecodeError:
        return False
    data["privacy"] = observed
    row.data_json = json.dumps(data)
    stored["privacy"] = observed
    return True


def _privacy_to_version(stored_privacy: Any, observed: str, *, video_id: str) -> str:
    """The privacy value to hand the rescan engine.

    ``observed`` when a difference is safe to record as a creator change,
    otherwise the value already stored - handing back what is there is how
    "write nothing for this field" is expressed to an engine whose whole job
    is to diff.
    """
    if not isinstance(stored_privacy, str):
        return observed
    current = stored_privacy.strip().lower()
    if current not in _VIDEO_PRIVACY_TIERS:
        # No usable prior claim to contradict. _fill_absent_privacy handles
        # this before the engine ever runs, so reaching here means the hole
        # is already filled or the value is junk we will not version against.
        return observed
    if current == observed:
        return observed
    if (
        current in _SHARED_PRIVACY_VOCABULARY
        and observed in _SHARED_PRIVACY_VOCABULARY
    ):
        return observed
    log.info(
        "metadata job for %s: privacy %s -> %s not versioned, the two "
        "sources disagree about member-gating rather than about the video",
        video_id, current, observed,
    )
    return current


def _worker_metadata_fields(meta: Any) -> Optional[Dict[str, Any]]:
    """Validate the worker's ``metadata`` object. None = unusable.

    Every field the rescan engine versions has to be present and the right
    type before we apply ANY of them. This is the rule the whole feature
    hangs off: a partial read is "we could not look", and the engine cannot
    tell the difference on its own - hand it a snippet with the description
    missing and it faithfully records that the creator deleted their
    description, complete with a history entry and a timespan. So a payload
    that is short of anything gets rejected whole. Half a metadata read is
    worth less than none.

    viewCount, durationSec and uploadDate are genuinely optional (a creator
    can hide stats, and a worker build may not carry the others). They are
    only carried through when present and well-formed; absent means the
    engine is never told about them, which it reads as "not looked at".
    """
    if not isinstance(meta, dict):
        return None

    title = meta.get("title")
    # An empty title is not a video with no title - there is no such thing
    # on YouTube. It is a parse that came back empty.
    if not isinstance(title, str) or not title.strip():
        return None
    description = meta.get("description")
    if not isinstance(description, str):
        return None
    tags = meta.get("tags")
    if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
        return None

    fields: Dict[str, Any] = {
        "title": title,
        "description": description,
        "tags": tags,
    }

    # Privacy is optional, not mandatory. yt-dlp answers "needs_auth" for an
    # age-restricted video whose title, description and tags all extract
    # fine, and the worker now omits the field rather than sending a value it
    # cannot stand behind. Sinking that whole good read over an unconfirmable
    # privacy label is what left every age-gated video with no upkeep at all.
    # Absent (or unrecognised) means the engine is never told about privacy,
    # which it already treats as "leave the stored value alone" - the same
    # handling view count gets. A present, recognised reading is carried
    # through and still versions normally.
    if "privacy" in meta:
        privacy = _normalize_worker_privacy(meta.get("privacy"))
        if privacy is not None:
            fields["privacy"] = privacy

    # bool is an int subclass in Python, so True would sail through the
    # isinstance checks below and land in the archive as a view count of 1.
    view_count = meta.get("viewCount")
    if isinstance(view_count, int) and not isinstance(view_count, bool):
        if view_count >= 0:
            fields["viewCount"] = view_count
    duration = meta.get("durationSec")
    if isinstance(duration, int) and not isinstance(duration, bool):
        if duration > 0:
            fields["durationSec"] = duration
    upload_date = meta.get("uploadDate")
    if isinstance(upload_date, str) and upload_date.strip():
        fields["uploadDate"] = upload_date.strip()
    return fields


def _metadata_api_item(
    fields: Dict[str, Any],
    *,
    video_id: str,
    stored: Dict[str, Any],
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    """Shape the worker's fields like a videos.list item.

    The rescan engine's job is to diff and version, and it already does that
    correctly for every field here - so rather than write a second engine
    that would drift from the first, we speak its input language. The engine
    then cannot tell (and does not need to know) whether the fields came off
    the Data API or off a machine signed in as the owner.

    Capture toggles are applied by substituting the value we ALREADY hold
    for any field the user has switched off. A field that equals what is
    stored is not a change, so the engine writes nothing for it - no value
    update, no snapshot. Blanking it instead would delete data the user paid
    to capture and record a fake edit while doing it. Toggles have always
    been forward-going here, never destructive.

    Privacy goes through _privacy_to_version first, because the engine takes
    any difference it is shown as a creator edit and the two sources this
    archive has do not agree on how to name member-gating.

    Thumbnails are deliberately never sent. The engine refreshes a thumbnail
    by fetching the bytes itself, and i.ytimg.com 404s for private videos -
    which is most of what this path exists for. A metadata job also gets no
    presigned thumbnail slot from /claim, so there is no trustworthy route
    for those bytes to reach us at all. Handing over a url our box cannot
    fetch does nothing at best, and at worst archives whatever placeholder
    image YouTube serves in its place over the real one.
    """
    description = fields["description"]
    if not settings.get("saveDescription", True):
        stored_description = stored.get("description")
        description = (
            stored_description if isinstance(stored_description, str) else ""
        )
    tags = fields["tags"]
    if not settings.get("saveTags", True):
        stored_tags = stored.get("tags")
        tags = stored_tags if isinstance(stored_tags, list) else []

    item: Dict[str, Any] = {
        "id": video_id,
        "snippet": {
            "title": fields["title"],
            "description": description,
            "tags": tags,
        },
    }
    # Only assert a privacy when the worker actually confirmed one. An absent
    # status block leaves the engine's privacyStatus read as None, which it
    # treats as "not looked at" and never versions - the right answer for an
    # age-gated video whose privacy yt-dlp could not confirm.
    if "privacy" in fields:
        item["status"] = {
            "privacyStatus": _privacy_to_version(
                stored.get("privacy"), fields["privacy"], video_id=video_id
            )
        }
    if settings.get("saveViewCount", True) and "viewCount" in fields:
        item["statistics"] = {"viewCount": fields["viewCount"]}
    return item


def _fill_absent_flat_fields(
    row: UserChannelVideo, fields: Dict[str, Any]
) -> List[str]:
    """Fill in uploadDate / durationSec when we hold no value for them.

    Neither is versioned, and neither can change on YouTube: a creator
    cannot re-date or re-cut a published video. So a difference between what
    we stored and what the worker reports is a difference in SOURCE, not an
    edit - rows discovered through the uploads playlist carry no date at all
    and the shared-pool fallback stamps them with the discovery time. Filling
    a hole is a strict improvement; overwriting a value would silently
    rewrite the archive with no history to show for it.

    Returns the names it filled. Caller owns the commit.
    """
    try:
        data = json.loads(row.data_json)
    except json.JSONDecodeError:
        return []
    filled: List[str] = []
    upload_date = fields.get("uploadDate")
    if upload_date and not data.get("uploadDate"):
        data["uploadDate"] = upload_date
        filled.append("uploadDate")
    duration = fields.get("durationSec")
    if duration and not data.get("durationSec"):
        data["durationSec"] = duration
        filled.append("durationSec")
    if filled:
        row.data_json = json.dumps(data)
    return filled


def _worker_comment_items(payload: Any) -> Optional[List[Dict[str, Any]]]:
    """Normalise the worker's ``comments`` payload into the engine dict keys
    app.comments_rescan reads. None = unusable (no ``comments`` object, or its
    ``items`` is not a list), which the caller fails loudly on rather than
    recording an empty pass as a successful sync.

    The worker sends camelCase nested under ``comments``; the store engine
    reads strict snake_case subscripts, so each item is mapped key-for-key. The
    three fields yt-dlp cannot expose - is_edited, viewer_rating_like,
    updated_at - are hard-defaulted (False / False / None) here, exactly as the
    payload contract fixes them, so the engine's subscripts never miss and so a
    worker can never assert an edit it has no way to detect. The engine's own
    text-hash diff is what actually flags edits. like_count coerces a possible
    null to 0.

    An item missing a string ``id`` is dropped, not defaulted: the id is the
    identity the whole diff keys off (and matches the OAuth-synced
    VideoComment.id format), so a comment we cannot name is one we can neither
    store nor match against what we already hold. ``parentId`` absent or empty
    normalises to None - a top-level comment, the yt-dlp parent=="root" case.
    """
    if not isinstance(payload, dict):
        return None
    block = payload.get("comments")
    if not isinstance(block, dict):
        return None
    raw_items = block.get("items")
    if not isinstance(raw_items, list):
        return None

    items: List[Dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        cid = raw.get("id")
        if not isinstance(cid, str) or not cid:
            continue
        # bool is an int subclass, so a stray True would pass an int check and
        # land as a like count of 1. Reject non-ints (incl. bool) to 0.
        like_count = raw.get("likeCount")
        if not isinstance(like_count, int) or isinstance(like_count, bool):
            like_count = 0
        parent_id = raw.get("parentId")
        # The worker maps yt-dlp parent=="root" to null, but defend the
        # contract here too: a build that ever sent the literal "root" would
        # otherwise store top-level comments under a bogus parent and break
        # threading. Absent, empty, or "root" all mean top-level.
        if not isinstance(parent_id, str) or not parent_id or parent_id == "root":
            parent_id = None
        author = raw.get("author")
        author_channel_id = raw.get("authorChannelId")
        text = raw.get("text")
        items.append(
            {
                "id": cid,
                "parent_id": parent_id,
                "author": author if isinstance(author, str) else "",
                "author_channel_id": (
                    author_channel_id
                    if isinstance(author_channel_id, str)
                    else ""
                ),
                "text": text if isinstance(text, str) else "",
                "like_count": like_count,
                "is_edited": False,
                "viewer_rating_like": False,
                "published_at": raw.get("publishedAt"),
                # yt-dlp exposes no edit timestamp, so the worker cannot stand
                # behind one; hard-defaulted to null like is_edited above.
                "updated_at": None,
            }
        )
    return items


def _comments_allow_deletions(
    *, complete: Any, reported_total: Any, fetched_count: int
) -> bool:
    """Whether a comment snapshot is trustworthy enough to soft-delete the
    comments it is missing. Safety guards 2 and 3, and nothing here can ever
    cause a deletion - it only ever decides whether one is permitted.

    Guard 2 (completeness): start from the worker's flag, which it sets true
    only when it used cookies, imposed no comment cap and yt-dlp exited 0. A
    false flag - or a non-bool, which we distrust as false - means
    insert/update-only, no deletions.

    Guard 3 (sanity ratio): even a "complete" fetch is refused for deletion
    when it came back implausibly short against yt-dlp's reported comment_count,
    which is how a bot-check interstitial that still exits 0 slips through.
    reportedTotal counts replies, so the honest comparison is the whole fetched
    set against it, and the ratio only bites once reportedTotal clears a floor
    so tiny threads aren't gated on reply-count noise. When reportedTotal is
    missing or not a usable count we cannot judge, so we do not manufacture a
    suppression - completeness alone stands.
    """
    if complete is not True:
        return False
    if isinstance(reported_total, int) and not isinstance(reported_total, bool):
        if (
            reported_total >= _COMMENTS_SANITY_MIN_REPORTED
            and fetched_count < reported_total * _COMMENTS_SANITY_MIN_RATIO
        ):
            return False
    return True


def _complete_comment_job(
    db: Session,
    *,
    job: SyncJob,
    payload: Optional[Dict[str, Any]],
    now: datetime,
) -> Dict[str, Any]:
    """Apply a comment job's result through the shared comments store engine.

    Nothing here can, on its own, mark a comment deleted. Deletions run only
    when the worker certified the fetch complete AND the sanity ratio holds
    (_comments_allow_deletions) AND the comment was already missing one cadence
    ago (the debounce the engine applies via deletion_grace). Anything short of
    all three is insert/update-only. A false "your comment was deleted" is the
    worst thing this feature can produce, so the default is to add and edit,
    never remove.

    Like _complete_metadata_job, this fails loudly rather than record a no-op
    as done: a payload we cannot read, or a video row we do not hold, is a
    failure, not an empty success. It owns its own commit and returns before
    the file/status/captions body of complete_sync_job, touching only the
    comment rows and this video's comment-sync clock.
    """
    items = _worker_comment_items(payload)

    row = (
        db.query(UserChannelVideo)
        .filter_by(
            user_id=job.user_id,
            channel_id=job.channel_id,
            video_id=job.video_id,
        )
        .first()
    )

    if items is None or row is None:
        reason = (
            "no usable comments in the completion payload"
            if items is None
            else "no video row for this job"
        )
        log.warning(
            "comment job %s for %s: %s - wrote nothing",
            job.id, job.video_id, reason,
        )
        job.status = "failed"
        job.error = f"Comment job wrote nothing: {reason}."
        job.finished_at = now
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Comment job wrote nothing: {reason}.",
        )

    # _worker_comment_items already proved payload and payload["comments"] are
    # dicts, so these reads are safe.
    block = payload["comments"]
    allow_deletions = _comments_allow_deletions(
        complete=block.get("complete"),
        reported_total=block.get("reportedTotal"),
        fetched_count=len(items),
    )

    # Deletion debounce (guard 4). Sized from the channel's own comments
    # cadence, the same setting the cron reads, with the slow quarterly default
    # when it is absent or retired. The engine only soft-deletes a missing
    # comment whose last_seen_at is already older than this, so a single
    # truncated fetch that slipped past guards 2 and 3 still cannot strand a
    # "deleted" mark on its own.
    ch_row = db.get(UserChannel, (job.user_id, job.channel_id))
    cadence = _COMMENTS_DEFAULT_CADENCE
    if ch_row is not None:
        try:
            ch_settings = (json.loads(ch_row.data_json) or {}).get(
                "settings"
            ) or {}
        except (json.JSONDecodeError, TypeError):
            ch_settings = {}
        cadence = ch_settings.get("commentsRefreshFrequency") or cadence
    deletion_grace = timedelta(
        days=_COMMENTS_CADENCE_DAYS.get(
            cadence, _COMMENTS_CADENCE_DAYS[_COMMENTS_DEFAULT_CADENCE]
        )
    )

    # is_by_uploader is author_channel_id == the video's channel owner. The
    # OAuth cron resolves that owner as the channel's YouTube id, and
    # job.channel_id is the same value (SyncJob.channel_id is the YouTube
    # channel id the job was enqueued against), so we resolve it identically.
    channel_owner_id = job.channel_id

    stats = comments_rescan.apply_comment_snapshot(
        db,
        row=row,
        api_comments=items,
        channel_owner_id=channel_owner_id,
        allow_deletions=allow_deletions,
        deletion_grace=deletion_grace,
    )

    # Bump the cron's per-video cadence gate so a worker sync counts as this
    # video's comment refresh and the OAuth cron does not immediately re-pull
    # it. The engine owns per-comment last_seen_at; this is the per-video clock,
    # the exact twin of last_metadata_sync_at above.
    row.last_comments_sync_at = now

    job.status = "done"
    job.progress = 1.0
    job.finished_at = now
    db.commit()

    log.info(
        "comment job %s for %s: allow_deletions=%s stats=%s",
        job.id, job.video_id, allow_deletions, stats,
    )
    return {
        "id": job.id,
        "fileSizeBytes": 0,
        "kind": job.kind,
        "allowDeletions": allow_deletions,
        "comments": stats,
    }


def _complete_metadata_job(
    db: Session,
    *,
    job: SyncJob,
    payload: Optional[Dict[str, Any]],
    now: datetime,
) -> Dict[str, Any]:
    """Apply a metadata job's result through the versioned rescan engine.

    Nothing here can mark a video removed. The worker not finding a video is
    reported through /fail, and a failed look is never evidence of a
    deletion - that verdict has one owner (the two-strike detector in
    metadata_rescan) and one input, and this is not it. A false "your video
    was deleted" email is the worst thing this system can produce.
    """
    meta = payload.get("metadata") if isinstance(payload, dict) else None
    fields = _worker_metadata_fields(meta)

    row = (
        db.query(UserChannelVideo)
        .filter_by(
            user_id=job.user_id,
            channel_id=job.channel_id,
            video_id=job.video_id,
        )
        .first()
    )
    stored: Optional[Dict[str, Any]] = None
    if row is not None:
        try:
            stored = json.loads(row.data_json)
        except json.JSONDecodeError:
            stored = None

    if fields is None or row is None or not isinstance(stored, dict):
        # Fail loudly rather than record a job that did nothing as done. The
        # unreadable-row case is in here on purpose: the engine parses
        # data_json without a guard, and a row we cannot read is not a row we
        # are going to start writing to.
        reason = (
            "no usable metadata in the completion payload"
            if fields is None
            else "no readable video row for this job"
        )
        log.warning(
            "metadata job %s for %s: %s - wrote nothing",
            job.id, job.video_id, reason,
        )
        job.status = "failed"
        job.error = f"Metadata job wrote nothing: {reason}."
        job.finished_at = now
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Metadata job wrote nothing: {reason}.",
        )

    ch_row = db.get(UserChannel, (job.user_id, job.channel_id))
    ch_settings: Dict[str, Any] = {}
    if ch_row is not None:
        try:
            ch_settings = (json.loads(ch_row.data_json) or {}).get(
                "settings"
            ) or {}
        except (json.JSONDecodeError, TypeError):
            ch_settings = {}

    # The worker just read this video while signed in as its owner, which is
    # the strongest sighting we can get - stronger than the public listing,
    # and the only one available for a sealed video. Clear any absence
    # strikes it had banked before diffing.
    metadata_rescan._record_sighting(row)

    # Must run before the engine reads data_json back, so the engine sees a
    # row whose privacy already matches and writes no snapshot for it. Only
    # when the worker confirmed a privacy: an age-gated video sends none, and
    # there is nothing to fill a hole with.
    privacy_filled = (
        _fill_absent_privacy(row, stored, fields["privacy"])
        if "privacy" in fields
        else False
    )

    # Reaching into the engine's private helpers rather than adding a second
    # one. _apply_api_item_to_row is the only entry point that takes an
    # already-fetched item; everything public above it calls the Data API
    # itself, which is precisely what we cannot do for these videos. If the
    # engine grows a public seam that takes a field dict, this call and
    # _metadata_api_item collapse into it.
    changes = metadata_rescan._apply_api_item_to_row(
        db,
        row=row,
        api_item=_metadata_api_item(
            fields,
            video_id=job.video_id,
            stored=stored,
            settings=ch_settings,
        ),
        now=now,
        settings=ch_settings,
    )
    filled = _fill_absent_flat_fields(row, fields)
    if privacy_filled:
        filled.append("privacy")
    # Bounds the open-ended "current value active since X" tail the same way
    # the batch rescan does. The engine leaves this to its caller.
    row.last_metadata_sync_at = now

    job.status = "done"
    job.progress = 1.0
    job.finished_at = now
    db.commit()

    log.info(
        "metadata job %s for %s: changed=%s filled=%s",
        job.id, job.video_id, sorted(changes), filled,
    )
    return {
        "id": job.id,
        "fileSizeBytes": 0,
        "kind": job.kind,
        "changed": sorted(changes),
        "filled": filled,
    }


@router.post("/sync-jobs/{job_id}/complete")
def complete_sync_job(
    job_id: str,
    payload: Optional[Dict[str, Any]] = Body(default=None),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Client signals a job is done. Four flavors based on job.kind:

    - kind='video' (default): we HEAD the expected mp4 in R2, flip
      the video row to archived, save probe metadata + captions.
    - kind='captions': captions-only backfill. We do NOT HEAD the
      mp4 (the worker never uploaded one), don't touch video status
      or localPath. ONLY the captions block is updated.
    - kind='metadata': field refresh for a video we already hold,
      handled entirely by _complete_metadata_job above and returning
      before any of the body below. It touches no file, no status and
      no captions block - only the versioned fields, through the
      shared rescan engine. Body: ``{"metadata": {title, description,
      tags, privacy, viewCount, durationSec, thumbnailUrl,
      uploadDate}}``.
    - kind='comments': comment-thread refresh for a video we already
      hold, handled entirely by _complete_comment_job above and
      returning before the body below. Touches no file and no video
      status - only the VideoComment rows, through the shared comments
      store engine, and this video's last_comments_sync_at clock. Body:
      ``{"comments": {complete, reportedTotal, items: [{id, parentId,
      author, authorChannelId, text, likeCount, isEdited,
      viewerRatingLike, publishedAt, updatedAt}]}}``.

    Optional payload (snake_case from the Rust worker):
      - video_resolution:    "1920x1080"     (video kind only)
      - video_codec:         "h264"
      - video_bitrate_kbps:  6420
      - video_fps:           29.97
      - audio_codec:         "aac"
      - audio_bitrate_kbps:  192
      - container_format:    "mp4"
      - sha256:              "<hex>"
      - caption_languages:   ["en", "es"]    (always present; [] if no
                                              manual captions)

    Every field is optional so the worker can ship even when ffprobe
    isn't installed; missing fields don't overwrite existing data.
    """
    job = db.get(SyncJob, job_id)
    if job is None or job.user_id != current.id:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.claimed_by != current.id or job.status != "running":
        raise HTTPException(status_code=409, detail="Job not claimed by you.")

    now = datetime.now(timezone.utc)
    file_size = 0

    # Metadata jobs are self-contained: they own their own commit and share
    # none of the file/status/captions bookkeeping below. Dispatching here
    # rather than threading a third kind through the body keeps the two
    # shipped kinds byte-for-byte unchanged.
    if job.kind == _METADATA_JOB_KIND:
        return _complete_metadata_job(db, job=job, payload=payload, now=now)

    # Comment jobs are self-contained the same way metadata jobs are: their own
    # commit, none of the file/status/captions bookkeeping below, and the same
    # rule that a comment fetch failing says nothing about the mp4 (the kind
    # guards in fail_sync_job / the progress mirror already keep "comments" off
    # the video row's status).
    if job.kind == _COMMENTS_JOB_KIND:
        return _complete_comment_job(db, job=job, payload=payload, now=now)

    if job.kind == "video":
        r2_key = r2_paths.video_key(job.user_id, job.video_id)
        meta = r2.head(r2_key, subject=job.user_id)
        if meta is None:
            raise HTTPException(
                status_code=400,
                detail="Upload not found in R2 — did the PUT actually succeed?",
            )
        file_size = int(meta.get("ContentLength") or 0)
        job.r2_key = r2_key
        job.file_size_bytes = file_size
        # Compute exact metadata bytes from the HEAD response so the
        # storage ledger row carries R2's real billable header size,
        # not the conservative 256-byte default.
        metadata_bytes = r2.metadata_bytes_for(
            content_type=meta.get("ContentType"),
            custom_meta=meta.get("Metadata"),
        )
        # Record the upload in the storage ledger so the bill cron sees
        # it. Idempotent — re-completes (which shouldn't happen) won't
        # double-record because the partial UNIQUE index covers the
        # active row at this key.
        storage_ledger.record_object(
            db,
            user_id=job.user_id,
            r2_key=r2_key,
            byte_count=file_size,
            kind="video",
            uploaded_at=now,
            metadata_bytes=metadata_bytes,
        )

    job.status = "done"
    job.progress = 1.0
    job.finished_at = now

    video = (
        db.query(UserChannelVideo)
        .filter_by(
            user_id=job.user_id,
            channel_id=job.channel_id,
            video_id=job.video_id,
        )
        .first()
    )
    if video:
        try:
            data = json.loads(video.data_json)
        except json.JSONDecodeError:
            data = {}

        # Video-kind jobs flip the row to archived + stamp video
        # bytes-related fields. Captions-kind jobs leave all of that
        # alone - they only touch the captions block.
        if job.kind == "video":
            data["status"] = "archived"
            data["localPath"] = r2_paths.video_key(job.user_id, job.video_id)
            data["fileSizeBytes"] = file_size
            data["archivedAt"] = now.isoformat()
            data.pop("syncProgress", None)

            # Stamp the channel-level quality settings at archive time
            # so the UI can detect when a video is locked to a lower
            # quality than the user's current preference and show an
            # "Outdated" pill / re-archive prompt.
            ch_row = db.get(
                UserChannel, (job.user_id, job.channel_id)
            )
            # Always defined: the metadata-capture toggles below read this
            # outside the `ch_row is not None` guard, and an absent legacy
            # row must not blow up the whole completion with a NameError.
            ch_settings: Dict[str, Any] = {}
            if ch_row is not None:
                try:
                    ch_settings = (
                        json.loads(ch_row.data_json).get("settings") or {}
                    )
                except json.JSONDecodeError:
                    ch_settings = {}
                if ch_settings.get("maxResolution"):
                    data["archivedMaxResolution"] = ch_settings["maxResolution"]
                if ch_settings.get("codecPreference"):
                    data["archivedCodecPreference"] = ch_settings["codecPreference"]

            if isinstance(payload, dict):
                probe_map = {
                    "video_resolution": "videoResolution",
                    "video_codec": "videoCodec",
                    "video_bitrate_kbps": "videoBitrateKbps",
                    "video_fps": "videoFps",
                    "audio_codec": "audioCodec",
                    "audio_bitrate_kbps": "audioBitrateKbps",
                    "container_format": "videoFormat",
                    "sha256": "fileSha256",
                }
                for snake, camel in probe_map.items():
                    if payload.get(snake) is not None:
                        data[camel] = payload[snake]

                # Real YouTube upload date from yt-dlp's info-json. Videos
                # discovered via the uploads playlist (owner-private ones)
                # arrive with no date, so the shared-pool row falls back to
                # "now" and every such video ends up sharing a discovery
                # timestamp - which makes sorting by upload date useless.
                # Backfill both models the first time we learn the truth.
                # Worker-captured thumbnail. It PUT the bytes to the
                # presigned slot from /claim; we just record the key + size
                # so the grid can presign it and the bill cron sees it.
                thumb_bytes = payload.get("thumbnail_bytes")
                if isinstance(thumb_bytes, int) and thumb_bytes > 0:
                    tkey = r2_paths.thumb_key(job.user_id, job.video_id)
                    video.thumbnail_r2_key = tkey
                    video.thumbnail_size_bytes = thumb_bytes
                    storage_ledger.record_object(
                        db,
                        user_id=job.user_id,
                        r2_key=tkey,
                        byte_count=thumb_bytes,
                        kind="thumbnail",
                        uploaded_at=now,
                        metadata_bytes=r2.metadata_bytes_for(
                            content_type="image/jpeg"
                        ),
                    )

                # Full record from the worker's info-json. For a channel
                # tracked by URL there's no OAuth and therefore no API path,
                # so this is the ONLY source of description / tags / counts —
                # without it we'd archive the file and lose everything
                # around it. Each field still honors its capture toggle.
                if ch_settings.get("saveDescription", True):
                    desc = payload.get("description")
                    if isinstance(desc, str) and desc:
                        data["description"] = desc
                if ch_settings.get("saveTags", True):
                    tags = payload.get("tags")
                    if isinstance(tags, list):
                        data["tags"] = [t for t in tags if isinstance(t, str)]
                if ch_settings.get("saveViewCount", True):
                    vc = payload.get("view_count")
                    if isinstance(vc, int) and vc >= 0:
                        data["viewCount"] = vc
                dur = payload.get("duration_sec")
                if isinstance(dur, int) and dur > 0:
                    data["durationSec"] = dur

                real_upload = payload.get("upload_date")
                if isinstance(real_upload, str) and real_upload:
                    data["uploadDate"] = real_upload
                    try:
                        from datetime import date as _date  # noqa: WPS433

                        y, m, d = real_upload.split("-")
                        published = datetime(
                            int(y), int(m), int(d), tzinfo=timezone.utc
                        )
                        shared = (
                            db.query(Video)
                            .filter(Video.youtube_id == job.video_id)
                            .one_or_none()
                        )
                        if shared is not None:
                            shared.published_at = published
                    except (ValueError, TypeError):
                        log.warning(
                            "unparseable upload_date %r for %s",
                            real_upload, job.video_id,
                        )

                # Real privacy from yt-dlp's `availability` - the only thing
                # that distinguishes a public upload (Open) from a private /
                # members one (Sealed). Without it, discovery's "private"
                # placeholder would seal every video. Unknown/missing leaves
                # the existing value untouched (safe default stays private),
                # and this feeds record_synced_video below via data_json.
                mapped_privacy = _privacy_from_availability(
                    payload.get("availability")
                )
                if mapped_privacy:
                    data["privacy"] = mapped_privacy

        # Captions block - written on BOTH kinds (a video sync also
        # reports captions, since they're fetched in the same yt-dlp
        # invocation). caption_languages is always sent by an updated
        # worker, even when empty - empty list = 'we checked, no
        # manual captions exist'. Skip writing only when the worker
        # didn't include the field (older worker build). Stored as a
        # flat top-level field on the video row so GET endpoints and
        # the downloadable metadata.json see it without nested digging.
        if isinstance(payload, dict) and "caption_languages" in payload:
            langs = payload.get("caption_languages") or []
            if isinstance(langs, list):
                data["captionLanguages"] = [
                    str(x) for x in langs if isinstance(x, str)
                ]
                # Record each caption in the storage ledger. We HEAD R2
                # to get the exact size the worker uploaded (each
                # caption is its own PUT via /caption-upload-url, so
                # the worker has no easy way to send sizes here).
                # Missing files are silently skipped — the worker
                # shouldn't report a language whose upload failed,
                # but defend anyway.
                for lang in data["captionLanguages"]:
                    cap_key = r2_paths.caption_key(
                        job.user_id, job.video_id, lang
                    )
                    cap_meta = r2.head(cap_key, subject=job.user_id)
                    if cap_meta is None:
                        log.warning(
                            "complete_sync_job: caption %s missing in R2 for job %s",
                            cap_key,
                            job.id,
                        )
                        continue
                    cap_size = int(cap_meta.get("ContentLength") or 0)
                    if cap_size <= 0:
                        continue
                    cap_metadata_bytes = r2.metadata_bytes_for(
                        content_type=cap_meta.get("ContentType"),
                        custom_meta=cap_meta.get("Metadata"),
                    )
                    storage_ledger.record_object(
                        db,
                        user_id=job.user_id,
                        r2_key=cap_key,
                        byte_count=cap_size,
                        kind="caption",
                        uploaded_at=now,
                        metadata_bytes=cap_metadata_bytes,
                    )

        video.data_json = json.dumps(data)

    # ---- Shared-pool mirror write (Phase 4a) -----------------------
    # Keep the new-model rows in sync with the legacy UserChannelVideo
    # update. Idempotent: re-completes (which shouldn't normally
    # happen) just touch the existing row. We extract the same
    # fields we'd write to data_json, but routed through app.archive
    # so the Channel + Subscription + Ownership rows get cascade-
    # ensured at the same time.
    if video and job.kind == "video":
        try:
            data_for_archive = json.loads(video.data_json) if video.data_json else {}
        except json.JSONDecodeError:
            data_for_archive = {}
        # Parse the published_at if present so the Video row carries
        # the real upload date.
        from app import archive as archive_lib  # noqa: WPS433
        from datetime import datetime as _dt  # noqa: WPS433

        def _parse_iso(s):
            if not s or not isinstance(s, str):
                return None
            try:
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                dt = _dt.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                return None

        ch_title = None
        ch_handle = None
        ch_row_for_meta = db.get(UserChannel, (job.user_id, job.channel_id))
        if ch_row_for_meta is not None:
            try:
                cd = json.loads(ch_row_for_meta.data_json) or {}
                ch_title = cd.get("title") or cd.get("name")
                ch_handle = cd.get("handle") or cd.get("customUrl")
            except Exception:
                pass

        archive_lib.record_synced_video(
            db,
            user_id=job.user_id,
            youtube_channel_id=job.channel_id,
            youtube_video_id=job.video_id,
            channel_title=ch_title,
            channel_handle=ch_handle,
            title=data_for_archive.get("title"),
            description=data_for_archive.get("description"),
            thumbnail_url=data_for_archive.get("thumbnailUrl"),
            published_at=_parse_iso(data_for_archive.get("uploadDate"))
            or _parse_iso(data_for_archive.get("publishedAt")),
            duration_seconds=data_for_archive.get("durationSec"),
            # Only reachable when the row's json was empty or unparseable, so
            # this is "we could not read the tier", not "the tier is public".
            # Seal it: this value decides Open vs Sealed for every other
            # subscriber, and an unread row must not publish a video.
            privacy=str(
                data_for_archive.get("privacy") or "private"
            ).lower(),
            r2_key=r2_key,
            bytes_stored=file_size,
            google_user_id=(
                ch_row_for_meta.google_user_id
                if ch_row_for_meta is not None
                else None
            ),
            # Mirror the full rich data_json blob so the YouTube page
            # can render videoResolution, captionLanguages, tags etc.
            # straight from Video.metadata_json after the read-route
            # cutover.
            metadata_json=json.dumps(data_for_archive),
        )

    db.commit()

    return {"id": job.id, "fileSizeBytes": file_size, "kind": job.kind}


@router.post("/sync-jobs/retry-failed")
def retry_failed_sync_jobs(
    db: Session = Depends(get_db),
    current: User = Depends(get_paid_user),
) -> Dict[str, int]:
    """Re-enqueue every video whose most recent sync_job is in 'failed'
    state (with no pending/running successor). Called by the worker app
    on startup so the user doesn't have to click Retry on each card
    after fixing whatever caused the original failures.

    Strategy:
      - Find all distinct video_ids the user has for failed sync_jobs
      - Subtract any that already have a pending/running job
      - For each remaining, insert a fresh pending sync_job and reset
        the matching UserChannelVideo.status from 'failed' to
        'discovered' so the UI stops claiming it failed during the
        retry window

    No retry counter / cap for now. A genuinely-broken video will
    re-fail on every worker startup, which is loud but not silent -
    the user sees it failing repeatedly and can investigate.

    Only video-kind failures are retried, because a video-kind job is the
    only thing this endpoint knows how to create. It has always inserted one
    with the default kind whatever the failed job was, so a failed captions
    or metadata job - neither of which downloads a file - would come back as
    a full re-download of the mp4 on the next worker startup. That is a lot
    of the user's bandwidth spent on work nobody asked for, and with
    metadata jobs it would happen every time one failed.
    """
    failed_video_ids = {
        vid
        for (vid,) in (
            db.query(SyncJob.video_id)
            .filter(
                SyncJob.user_id == current.id,
                SyncJob.status == "failed",
                # NULL kind means a row that predates the column, and every
                # one of those was a video job.
                or_(SyncJob.kind == "video", SyncJob.kind.is_(None)),
            )
            .all()
        )
    }
    if not failed_video_ids:
        return {"retried": 0}

    in_flight = {
        vid
        for (vid,) in (
            db.query(SyncJob.video_id)
            .filter(
                SyncJob.user_id == current.id,
                SyncJob.video_id.in_(failed_video_ids),
                SyncJob.status.in_(["pending", "running"]),
            )
            .all()
        )
    }
    to_retry = failed_video_ids - in_flight

    # Pull the matching UserChannelVideo rows so we can both look up
    # channel_id for the new sync_job and reset the visible status.
    ucv_rows = (
        db.query(UserChannelVideo)
        .filter(
            UserChannelVideo.user_id == current.id,
            UserChannelVideo.video_id.in_(to_retry),
        )
        .all()
    )

    retried = 0
    for ucv in ucv_rows:
        # Skip videos that are already successfully archived. A failed
        # sync_job from earlier (e.g. before a yt-dlp flag fix) stays
        # in the DB forever even after a later attempt succeeds, so
        # without this guard retry-failed would re-enqueue archived
        # videos for re-download on every worker startup.
        try:
            data = json.loads(ucv.data_json)
        except json.JSONDecodeError:
            data = {}
        if data.get("status") == "archived":
            continue
        # Enqueue fresh sync_job
        db.add(
            SyncJob(
                user_id=current.id,
                channel_id=ucv.channel_id,
                video_id=ucv.video_id,
            )
        )
        # Flip the visible status off 'failed' so the UI doesn't
        # mislead. The worker_loop will set it to 'archived' on
        # success or back to 'failed' on the next /fail call.
        # `data` is already parsed above for the archived-skip check.
        if data.get("status") == "failed":
            data["status"] = "discovered"
            data.pop("syncProgress", None)
            ucv.data_json = json.dumps(data)
        retried += 1
    db.commit()
    return {"retried": retried}


@router.post("/sync-jobs/{job_id}/fail", status_code=status.HTTP_204_NO_CONTENT)
def fail_sync_job(
    job_id: str,
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Response:
    """Client reports a job failure with an error message."""
    job = db.get(SyncJob, job_id)
    if job is None or job.user_id != current.id:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.claimed_by != current.id or job.status != "running":
        raise HTTPException(status_code=409, detail="Job not claimed by you.")

    err = payload.get("error") or "Unknown error"
    if not isinstance(err, str):
        err = str(err)

    job.status = "failed"
    job.error = err[:500]
    job.finished_at = datetime.now(timezone.utc)

    # Mirror to the video row so the UI shows it - but only for the kind of
    # job that is actually fetching the file. ``status`` on the video row
    # means "state of this video's archive", and a captions or metadata job
    # failing says nothing about the mp4: it is still there, still playable,
    # still what the user paid us to hold. Writing "failed" over it turns
    # "we could not look" into a claim about the archive itself, which is
    # the one thing this path must never do. It also used to stick: nothing
    # clears that status once retry-failed stopped re-enqueueing non-video
    # failures, so an archived video would show a failed download forever.
    if job.kind in (None, "video"):
        video = (
            db.query(UserChannelVideo)
            .filter_by(
                user_id=job.user_id,
                channel_id=job.channel_id,
                video_id=job.video_id,
            )
            .first()
        )
        if video:
            try:
                data = json.loads(video.data_json)
            except json.JSONDecodeError:
                data = {}
            data["status"] = "failed"
            data.pop("syncProgress", None)
            video.data_json = json.dumps(data)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------- OAuth-authenticated channel import ----------
#
# Phase 2 of the MVP: once a user has connected their Google account, this
# endpoint pulls their channel + every video they own (public, unlisted,
# private, members) via the official YouTube Data API and persists the
# catalog. Actual file downloads happen later in the desktop app — this
# just builds the metadata layer.


# The platform baseline every new user starts with, set from the owner's own
# configured New-channel-defaults (2026-07-23). Now a COMPLETE settings blob -
# it previously omitted the history and notify flags and leaned on frontend
# normalization to fill them, which meant the real default for those keys
# lived in two places. Kept in sync with defaultChannelSettings in
# src/lib/mockData.ts.
_DEFAULT_CHANNEL_SETTINGS: Dict[str, Any] = {
    # New channels start ACTIVE. This reverses an earlier decision to have
    # them start paused pending a manual flip, because it cannot coexist
    # with the model the product now has: adding a channel queues its
    # whole back catalogue. A paused-by-default channel queues nothing, so
    # "I added my channel to a backup service" would still have backed up
    # nothing until the user found a toggle nobody told them about - which
    # is exactly the trap that hid a broken pipeline for weeks.
    #
    # Pausing is still one click away, and it is still per channel. It is
    # just no longer the state you land in by accident.
    "active": True,
    "downloadNewVideos": True,
    # Best-available quality: largest files, but the fullest archive.
    "maxResolution": "source",
    "codecPreference": "compat",
    "saveThumbnail": True,
    "saveViewCount": True,
    "saveDescription": True,
    "saveTags": True,
    "saveCaptions": True,
    "saveThumbnailHistory": True,
    "saveViewCountHistory": True,
    "saveDescriptionHistory": True,
    "saveTagsHistory": True,
    "saveCaptionsHistory": True,
    "saveChannelAvatarHistory": True,
    "saveChannelAboutHistory": True,
    "saveChannelStatsHistory": True,
    "includeMetadataOnVideoSync": True,
    "metadataRefreshFrequency": "monthly",
    "syncComments": True,
    # "manual" is a retired cadence the frontend coerces away on read;
    # seeding it would hand every new channel an invalid value.
    "commentsRefreshFrequency": "monthly",
    "saveChannelStatsSnapshots": True,
    "saveChannelAvatar": True,
    "saveChannelAbout": True,
    "showStatusBadges": True,
    "useStatusColorBorder": True,
    "cardMetaFields": ["uploadDate", "fileSize", "duration", "type"],
    # Integrity alerts off by default: the notification send paths are still
    # being proven, so off avoids promising an email we might not deliver.
    "notifyVideoDeleted": False,
    "notifyChannelTerminated": False,
    "notifyOauthDisconnected": False,
    "notifyNewUpload": False,
    "notifyMonthlyDigest": False,
    "filterPresets": [
        {
            "id": "all",
            "label": "All",
            "locked": True,
            "search": "",
            "visibilities": [],
            "types": [],
            "dateFrom": "",
            "dateTo": "",
            "sortDimension": "upload",
            "sortDirection": "desc",
            "viewMode": "grid",
        },
        {
            # "Archived" = held in ARCHIVE336 but not a normal public video on
            # YouTube: private, unlisted, members-only, or gone. The id is
            # frozen for compatibility with presets already saved in the DB.
            # Distinct from the header "Archived X/Y" stat (every copy we hold,
            # public included) - a known, accepted overlap.
            "id": "archived",
            "label": "Archived",
            "locked": False,
            "search": "",
            "visibilities": ["deleted", "private", "unlisted", "members"],
            "types": [],
            "dateFrom": "",
            "dateTo": "",
            "sortDimension": "upload",
            "sortDirection": "desc",
            "viewMode": "grid",
        },
    ],
}


# The settings a user may pre-configure in the "New channel defaults" panel
# (stored per-user in UserYouTubeSettings). Whitelisted: the blob arrives
# through an authenticated PUT but is still client-authored JSON, and only
# recognised setting keys may seed every future channel.
_NEW_CHANNEL_DEFAULT_KEYS = frozenset(_DEFAULT_CHANNEL_SETTINGS.keys()) | {
    "saveThumbnailHistory",
    "saveViewCountHistory",
    "saveDescriptionHistory",
    "saveTagsHistory",
    "saveCaptionsHistory",
    "saveChannelAvatarHistory",
    "saveChannelAboutHistory",
    "saveChannelStatsHistory",
    "notifyVideoDeleted",
    "notifyChannelTerminated",
    "notifyOauthDisconnected",
    "notifyNewUpload",
    "notifyMonthlyDigest",
}


def _new_channel_settings(db: Session, user_id: str) -> Dict[str, Any]:
    """Per-channel settings for a channel being added (or re-added).

    App baseline overlaid with the user's saved New-channel-defaults. This
    is what makes that settings panel real: before it existed here, every
    add path honored exactly one of its keys ("active") and hardcoded the
    rest, so a user who configured defaults got a channel that ignored
    almost all of them.

    Removal wipes per-channel settings by design - restoring a channel
    inside the 30-day grace window reuses the archived data but goes
    through this function again, so it comes back wearing the CURRENT
    defaults, not the settings it had when it was removed. That is the
    contract the defaults panel promises: remove a channel, add it back,
    get your defaults.
    """
    settings = json.loads(json.dumps(_DEFAULT_CHANNEL_SETTINGS))
    row = db.get(UserYouTubeSettings, user_id)
    if row is not None:
        try:
            saved = json.loads(row.settings_json)
        except json.JSONDecodeError:
            saved = None
        if isinstance(saved, dict):
            for key, value in saved.items():
                if key in _NEW_CHANNEL_DEFAULT_KEYS:
                    settings[key] = value
    return settings


def _parse_iso_duration(s: str) -> int:
    """Parse a YouTube ISO-8601 duration like 'PT1H2M3S' into seconds.

    YouTube uses a strict subset (no fractional seconds, no months/years
    for individual videos), so a tiny regex parser is enough — saves
    pulling in isodate as a dep.
    """
    import re

    m = re.match(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$", s or "")
    if not m:
        return 0
    days, hours, minutes, seconds = m.groups()
    return (
        (int(days) if days else 0) * 86400
        + (int(hours) if hours else 0) * 3600
        + (int(minutes) if minutes else 0) * 60
        + (int(seconds) if seconds else 0)
    )


def _privacy_from_status(api_status: Optional[str]) -> Optional[str]:
    """Map the Data API's ``status.privacyStatus`` to our privacy tier.

    Returns None when the API didn't give us one (or gave a value we don't
    know), so callers keep whatever they already had rather than treating an
    absent field as a claim. This used to answer "public" in that case, which
    meant one partial API response could mark a private video public - the
    worst direction to be wrong in, since the tier is what seals a video.
    """
    s = (api_status or "").lower()
    if s in ("public", "unlisted", "private"):
        return s
    return None


def _video_type(item: Dict[str, Any], duration_sec: int) -> str:
    """Best-effort classify a video as livestream / short / video.

    YouTube Data API doesn't directly expose 'is this a Short' — we use
    the 60-second cutoff as a heuristic, which matches how YouTube itself
    categorizes Shorts in 2026. Livestreams are explicit.
    """
    snippet = item.get("snippet") or {}
    if snippet.get("liveBroadcastContent") in ("live", "upcoming"):
        return "livestream"
    if duration_sec > 0 and duration_sec <= 60:
        return "short"
    # Some past livestreams are flagged via liveStreamingDetails — we
    # don't fetch that part here, treat as regular video.
    return "video"


def _pick_thumbnail(snippet: Dict[str, Any]) -> str:
    thumbs = snippet.get("thumbnails") or {}
    for size in ("maxres", "standard", "high", "medium", "default"):
        url = (thumbs.get(size) or {}).get("url")
        if url:
            return url
    return ""


def _load_user_credentials(
    db: Session,
    user_id: str,
    google_user_id: Optional[str] = None,
) -> Optional[google_oauth.Credentials]:
    """Thin wrapper kept to avoid touching every call-site. Real logic
    lives in app.oauth_loader so cron scripts can reuse it without
    pulling in the whole routes module."""
    from app.oauth_loader import load_user_credentials
    return load_user_credentials(db, user_id, google_user_id)


def _first_google_user_id(db: Session, user_id: str) -> Optional[str]:
    """Return the google_user_id of the user's first connected account,
    or None if they have no connections. Step A still treats users as
    having one connection — this helper lets us stamp UserChannel rows
    on insert so step C/D can route per-channel sync to the right
    connection without a backfill."""
    row = (
        db.query(UserGoogleConnection)
        .filter(UserGoogleConnection.user_id == user_id)
        .order_by(UserGoogleConnection.connected_at.asc())
        .first()
    )
    return row.google_user_id if row else None


@router.post("/connected/import")
def import_connected_channel(
    google_user_id: Optional[str] = None,
    db: Session = Depends(get_db),
    current: User = Depends(get_paid_user),
) -> Dict[str, Any]:
    """Import the user's own YouTube channel + every video into the catalog.

    With multi-account support, the caller passes ``google_user_id`` as
    a query string parameter to indicate which connected account to
    import from. Omitted means "use the user's first connection" — kept
    for backwards-compat callers; the UI always passes it.

    Idempotent: re-running updates existing rows in place. Returns counts
    so the UI can surface "imported N new, updated M".
    """
    creds = _load_user_credentials(db, current.id, google_user_id)
    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Connect your YouTube account first.",
        )

    try:
        channel = google_oauth.fetch_my_channel(creds)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Couldn't reach YouTube.",
        )
    if channel is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Your Google account doesn't have a YouTube channel.",
        )

    channel_id = channel["id"]
    snippet = channel.get("snippet") or {}
    statistics = channel.get("statistics") or {}
    content_details = channel.get("contentDetails") or {}
    related = (content_details.get("relatedPlaylists") or {})
    uploads_playlist = related.get("uploads")

    # Build the Channel JSON the frontend expects.
    now_iso = datetime.now(timezone.utc).isoformat()
    handle = (snippet.get("customUrl") or "").strip()
    if handle and not handle.startswith("@"):
        handle = "@" + handle

    existing_channel_row = db.get(UserChannel, (current.id, channel_id))
    if existing_channel_row is not None:
        try:
            existing_data = json.loads(existing_channel_row.data_json)
        except json.JSONDecodeError:
            existing_data = {}
    else:
        existing_data = {}

    channel_payload: Dict[str, Any] = {
        "id": channel_id,
        "handle": handle or channel_id,
        "name": snippet.get("title") or "",
        "avatarUrl": _pick_thumbnail(snippet),
        "description": snippet.get("description") or "",
        "subscriberCount": int(statistics.get("subscriberCount") or 0),
        "videoCount": int(statistics.get("videoCount") or 0),
        "totalViews": int(statistics.get("viewCount") or 0),
        "country": snippet.get("country") or "",
        "joinedAt": snippet.get("publishedAt") or "",
        "links": existing_data.get("links") or [],
        "addedAt": existing_data.get("addedAt") or now_iso,
        "lastSyncedAt": now_iso,
        "terminatedAt": existing_data.get("terminatedAt"),
        "youtubeStatus": "available",
        # Three cases, deliberately distinct. A LIVE channel being
        # re-imported keeps its per-channel settings - re-import refreshes
        # identity data and must not nuke the user's tweaks. A channel in
        # the removed-grace window, and a brand-new one, both get the
        # user's CURRENT New-channel-defaults: removal wipes settings by
        # contract, so adding back means "as if new", same as the
        # track-by-url path.
        "settings": (
            existing_data.get("settings")
            if existing_channel_row is not None
            and existing_channel_row.removed_at is None
            and existing_data.get("settings")
            else _new_channel_settings(db, current.id)
        ),
    }

    # Upsert channel row. Stamp the specific google_user_id used for
    # the import so the sync worker later picks the right OAuth token
    # to refresh — important once the user has multiple connections.
    stamp_google_user_id = google_user_id or _first_google_user_id(
        db, current.id
    )
    if existing_channel_row is None:
        channel_row = UserChannel(
            user_id=current.id,
            channel_id=channel_id,
            google_user_id=stamp_google_user_id,
            data_json=json.dumps(channel_payload),
        )
        db.add(channel_row)
    else:
        existing_channel_row.data_json = json.dumps(channel_payload)
        if existing_channel_row.google_user_id is None:
            existing_channel_row.google_user_id = stamp_google_user_id
        # Re-importing a channel that was inside the soft-delete
        # grace window restores it. Same row, same archived data,
        # no purge.
        if existing_channel_row.removed_at is not None:
            existing_channel_row.removed_at = None
            # Resume billing for the restored channel's storage objects.
            storage_ledger.propagate_channel_restore(
                db, current.id, channel_id
            )
        channel_row = existing_channel_row
    _archive_channel_avatar(db, channel_row, channel_payload)

    # Pull every video. If there's no uploads playlist (channel exists but
    # has no uploads) skip the video import gracefully.
    imported_new = 0
    updated = 0
    # (row, thumbnail_url) pairs - filled in the loop below, drained
    # in parallel by _archive_thumbnails_parallel before commit.
    thumbnail_jobs: List["tuple[UserChannelVideo, str]"] = []
    # Resolve once: channel-level "what to capture at discovery" - same
    # gate applies to every video row in this import.
    capture_want = _capture_at_discovery(channel_payload["settings"])
    if uploads_playlist:
        try:
            video_ids = google_oauth.fetch_all_video_ids(creds, uploads_playlist)
            videos = google_oauth.fetch_video_details(creds, video_ids)
        except Exception:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Couldn't fetch videos from YouTube.",
            )

        # YouTube occasionally returns the same video_id twice in a single
        # fetch (videos in the uploads playlist more than once, or quirks
        # in fetch_video_details). Without dedup, two rows with identical
        # PKs queue up and the commit fails with UNIQUE constraint failed
        # on (user_id, channel_id, video_id). seen_ids guards the loop.
        seen_ids: set[str] = set()
        for v in videos:
            video_id = v.get("id")
            if not video_id:
                continue
            if video_id in seen_ids:
                continue
            seen_ids.add(video_id)

            v_snippet = v.get("snippet") or {}
            v_content = v.get("contentDetails") or {}
            v_stats = v.get("statistics") or {}
            v_status = v.get("status") or {}

            duration_sec = _parse_iso_duration(v_content.get("duration") or "")

            existing_video_row = db.get(
                UserChannelVideo, (current.id, channel_id, video_id)
            )
            if existing_video_row is not None:
                try:
                    existing_video = json.loads(existing_video_row.data_json)
                except json.JSONDecodeError:
                    existing_video = {}
            else:
                existing_video = {}

            video_payload: Dict[str, Any] = {
                "id": video_id,
                "channelId": channel_id,
                "title": v_snippet.get("title") or "",
                "description": v_snippet.get("description") or "",
                "uploadDate": v_snippet.get("publishedAt") or "",
                "durationSec": duration_sec,
                "thumbnailUrl": _pick_thumbnail(v_snippet),
                "status": existing_video.get("status") or "discovered",
                # Same rule as the sync path: no privacyStatus means we were
                # not told, so preserve the row's tier and seal an
                # unclassifiable new one rather than publishing it.
                "privacy": (
                    _privacy_from_status(v_status.get("privacyStatus"))
                    or existing_video.get("privacy")
                    or "private"
                ),
                "type": _video_type(v, duration_sec),
                "viewCount": int(v_stats.get("viewCount") or 0),
                "tags": v_snippet.get("tags") or [],
                "commentCount": int(v_stats.get("commentCount") or 0),
                "comments": existing_video.get("comments") or [],
                "captionLanguages": existing_video.get("captionLanguages") or [],
                "videoFormat": existing_video.get("videoFormat"),
                "videoResolution": existing_video.get("videoResolution"),
                "videoBitrateKbps": existing_video.get("videoBitrateKbps"),
                "localPath": existing_video.get("localPath"),
                "fileSizeBytes": existing_video.get("fileSizeBytes"),
                "firstSeenAt": existing_video.get("firstSeenAt") or now_iso,
                "archivedAt": existing_video.get("archivedAt"),
                "lastYoutubeCheckAt": now_iso,
                "deletedOnYoutubeAt": existing_video.get("deletedOnYoutubeAt"),
            }

            # Honor the includeMetadataOnVideoSync gate + per-field
            # toggles at discovery. Existing values on the row are
            # preserved either way - the gate is forward-going and
            # never destroys data we already paid to capture.
            _apply_discovery_gate(
                video_payload, existing_video, capture_want,
            )

            if existing_video_row is None:
                row = UserChannelVideo(
                    user_id=current.id,
                    channel_id=channel_id,
                    video_id=video_id,
                    data_json=json.dumps(video_payload),
                )
                db.add(row)
                imported_new += 1
            else:
                existing_video_row.data_json = json.dumps(video_payload)
                row = existing_video_row
                updated += 1

            # Queue this video's thumbnail for archiving if (a) it isn't
            # already saved in R2 and (b) the discovery gate says we
            # want it. Without the second check, includeMetadataOnVideoSync
            # = false would still pay the R2 storage cost for the image.
            if capture_want.get("thumbnailUrl") and not row.thumbnail_r2_key:
                thumb_url = video_payload.get("thumbnailUrl")
                if thumb_url:
                    thumbnail_jobs.append((row, thumb_url))

    _archive_thumbnails_parallel(db, thumbnail_jobs)
    db.commit()

    return {
        "channelId": channel_id,
        "channelTitle": channel_payload["name"],
        "videoCount": channel_payload["videoCount"],
        "importedNew": imported_new,
        "updated": updated,
    }


# ============================================================
# PubSubHubbub callback
# ------------------------------------------------------------
# Google's hub at https://pubsubhubbub.appspot.com/ posts here
# both for subscription verification (GET with hub.challenge) and
# for actual notifications (POST with an Atom feed of new entries).
# Single handler covers both; method discrimination lives inside
# the function so the route registration stays simple.
# ============================================================


@router.get("/pubsub-callback")
def pubsub_callback_verify(request: Request) -> Response:
    """Subscription verification step. The hub sends:

      hub.mode       = "subscribe" or "unsubscribe"
      hub.topic      = the feed URL we asked it to watch
      hub.challenge  = random string we must echo back as plain text
      hub.lease_seconds (optional)

    To confirm ownership of this callback URL, we just echo back the
    challenge in the response body with HTTP 200. Anything else
    (including missing params) -> 404 so the hub treats it as
    rejection.
    """
    mode = request.query_params.get("hub.mode")
    topic = request.query_params.get("hub.topic", "")
    challenge = request.query_params.get("hub.challenge")
    if mode not in ("subscribe", "unsubscribe") or not challenge:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    log.info("pubsub verify ok: mode=%s topic=%s", mode, topic[:80])
    return PlainTextResponse(challenge, status_code=status.HTTP_200_OK)


@router.post("/pubsub-callback")
async def pubsub_callback_notify(
    request: Request, db: Session = Depends(get_db)
) -> Response:
    """Notification step. The hub POSTs an Atom feed payload when a
    channel we subscribed to publishes (or updates) a video. We:

      1. Verify the X-Hub-Signature against PUBSUB_SECRET. Drop on
         mismatch - either someone forged a notification or a hub
         misconfig is sending us bad signatures; either way we don't
         want fake Video rows.
      2. Parse the Atom feed -> list of {channel_id, video_id, title,
         published_at} entries.
      3. For each entry, upsert a Video row via app.archive (creating
         the Channel if we somehow don't have it yet, though that
         shouldn't happen for legitimately subscribed channels).
      4. Return 200 so the hub doesn't retry. Any work we choose to
         defer happens through the existing sync-job pipeline -
         create_pending_sync_job() is called for new video_ids so the
         worker picks them up.
    """
    raw = await request.body()
    sig = request.headers.get("X-Hub-Signature")
    if not pubsub.verify_signature(raw, sig):
        log.warning(
            "pubsub notification signature mismatch (sig=%s, secret_configured=%s)",
            sig,
            bool(os.environ.get("PUBSUB_SECRET")),
        )
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    entries = pubsub.parse_notification(raw)
    if not entries:
        log.info("pubsub notification had no parseable entries")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    from app import archive as archive_lib
    from app import auto_download
    from app.models import Channel as _Channel

    inserted = 0
    new_by_channel: Dict[str, List[str]] = {}
    for entry in entries:
        channel_yt_id = entry["channel_id"]
        video_yt_id = entry["video_id"]
        if not channel_yt_id or not video_yt_id:
            continue

        # We should already have a Channel row for any feed we're
        # subscribed to. If not, the entry is spurious - log and
        # skip rather than create a stub.
        channel = (
            db.query(_Channel)
            .filter(_Channel.youtube_id == channel_yt_id)
            .one_or_none()
        )
        if channel is None:
            log.info(
                "pubsub notify for unknown channel %s; skipping",
                channel_yt_id,
            )
            continue

        existing = (
            db.query(Video)
            .filter(Video.youtube_id == video_yt_id)
            .one_or_none()
        )
        if existing is not None:
            # Edit notification or re-fire of an existing upload.
            # Refresh title and published_at if the new values
            # differ; nothing else to do (the worker pool will
            # re-sync when our scheduled metadata refresh hits).
            if entry["title"] and entry["title"] != existing.title:
                existing.title = entry["title"]
            if entry["published_at"]:
                existing.published_at = entry["published_at"]
            continue

        new_video = Video(
            channel_id=channel.id,
            youtube_id=video_yt_id,
            title=entry["title"] or video_yt_id,
            published_at=entry["published_at"]
            or datetime.now(timezone.utc),
            privacy_at_discovery="public",  # PubSub only fires for public
            privacy_current="public",
            visibility="open",
        )
        db.add(new_video)
        inserted += 1
        new_by_channel.setdefault(channel_yt_id, []).append(video_yt_id)
        log.info(
            "pubsub: new video %s on channel %s",
            video_yt_id,
            channel_yt_id,
        )

    # Queue the actual downloads for every subscriber with auto-download on.
    # This is what makes "Automatically sync" real - discovery alone never
    # downloaded anything. Best-effort: a failure here must not make us
    # return non-200 and have the hub retry the whole notification.
    queued = 0
    for ch_yt_id, vids in new_by_channel.items():
        try:
            queued += auto_download.auto_enqueue_for_channel(
                db, channel_youtube_id=ch_yt_id, video_ids=vids
            )
        except Exception:  # noqa: BLE001
            log.exception("auto-download enqueue failed for %s", ch_yt_id)

        # Activity notification (opt-in, off by default) for each subscriber.
        try:
            from app import notify as notify_lib  # noqa: WPS433

            subs = (
                db.query(UserChannel)
                .filter(
                    UserChannel.channel_id == ch_yt_id,
                    UserChannel.removed_at.is_(None),
                )
                .all()
            )
            for uc in subs:
                try:
                    ch_name = (
                        json.loads(uc.data_json) or {}
                    ).get("name") or ch_yt_id
                except (json.JSONDecodeError, TypeError):
                    ch_name = ch_yt_id
                for entry in entries:
                    if entry.get("channel_id") != ch_yt_id:
                        continue
                    if entry.get("video_id") not in vids:
                        continue
                    notify_lib.notify_new_upload(
                        db,
                        user_id=uc.user_id,
                        channel_youtube_id=ch_yt_id,
                        channel_name=ch_name,
                        video_title=entry.get("title") or "New video",
                        video_id=entry.get("video_id"),
                    )
        except Exception:  # noqa: BLE001
            log.exception("new-upload notification failed for %s", ch_yt_id)

    db.commit()
    log.info(
        "pubsub notification processed: %d new, %d total entries, %d download(s) queued",
        inserted,
        len(entries),
        queued,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
