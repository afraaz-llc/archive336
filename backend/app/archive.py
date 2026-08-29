"""Write-side helpers for the shared-pool archive.

Where ``app.access`` does the read-side rules ("can this user see
this video?"), this module does the writes — upserting Channel /
Video / UserChannelSubscription / ChannelOwnership rows when the
worker reports activity or when a user adds a channel via the
website.

Also home to the response-shape helpers (``channel_response_payload``,
``video_response_payload``) that translate new-model rows into the
exact JSON shape the frontend's YouTube page expects today. Keeps
the cross-schema translation in one place so the route handlers stay
short.

Everything here is idempotent: ``ensure_*`` functions return the
existing row when it's already there, or create + commit a fresh
one.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session

from app.models import (
    Channel,
    ChannelOwnership,
    UserChannelSubscription,
    visibility_for_privacy,
    Video,
)

log = logging.getLogger("archive336.archive")


def ensure_channel(
    db: Session,
    youtube_id: str,
    *,
    title: Optional[str] = None,
    handle: Optional[str] = None,
    thumbnail_url: Optional[str] = None,
) -> Channel:
    """Return the Channel for ``youtube_id``, creating it on the fly
    if missing. Optional fields are only filled in when we create a
    fresh row — existing rows keep their stored values (assumes a
    later sync-metadata flow refreshes them deliberately).
    """
    ch = (
        db.query(Channel)
        .filter(Channel.youtube_id == youtube_id)
        .one_or_none()
    )
    if ch is not None:
        return ch
    ch = Channel(
        youtube_id=youtube_id,
        title=title or f"Channel {youtube_id}",
        handle=handle,
        thumbnail_url=thumbnail_url,
    )
    db.add(ch)
    db.flush()
    return ch


def ensure_subscription(
    db: Session, user_id: str, channel_id: str
) -> UserChannelSubscription:
    """Return the (user, channel) subscription, creating it as
    active if absent. If a soft-deleted subscription exists
    (unsubscribed_at set), reactivate it (clear unsubscribed_at) —
    the user re-engaged within the grace, no reason to penalize.
    """
    sub = (
        db.query(UserChannelSubscription)
        .filter(
            UserChannelSubscription.user_id == user_id,
            UserChannelSubscription.channel_id == channel_id,
        )
        .one_or_none()
    )
    if sub is None:
        sub = UserChannelSubscription(
            user_id=user_id, channel_id=channel_id
        )
        db.add(sub)
        db.flush()
        return sub
    if sub.unsubscribed_at is not None:
        sub.unsubscribed_at = None
        db.flush()
    return sub


def soft_delete_subscription(
    db: Session, user_id: str, channel_id: str
) -> Optional[UserChannelSubscription]:
    """Mark the user's subscription to a channel as unsubscribed
    (sets unsubscribed_at). This is what drops the channel out of
    list_channels and stops the v2 storage meter for this user. The
    shared Channel + every Video row are left untouched — other users
    may still subscribe to the same channel.

    ``channel_id`` is the internal Channel.id (not the YouTube id).
    Returns the soft-deleted row, or None if there was no active
    subscription (already removed / never subscribed) — callers can
    treat None as a no-op.
    """
    sub = (
        db.query(UserChannelSubscription)
        .filter(
            UserChannelSubscription.user_id == user_id,
            UserChannelSubscription.channel_id == channel_id,
            UserChannelSubscription.unsubscribed_at.is_(None),
        )
        .one_or_none()
    )
    if sub is None:
        return None
    sub.unsubscribed_at = datetime.now(timezone.utc)
    db.flush()
    return sub


def ensure_ownership(
    db: Session,
    user_id: str,
    channel_id: str,
    *,
    google_user_id: str,
) -> ChannelOwnership:
    """Return the ChannelOwnership for (user, channel), creating it
    if missing. Reactivates a soft-revoked row the same way as the
    subscription helper, UNLESS the user revoked it themselves.

    The caller here is the worker reporting which channels it is
    signed in as, which it does on every app launch. That report is
    evidence of access, not a decision - so it can clear the machine
    bookkeeping column (revoked_at) but must never clear
    user_revoked_at. Only the explicit re-authenticate route does
    that. Without the guard the revoke control on the settings card
    would undo itself within minutes and the user would believe they
    revoked something they did not.
    """
    own = (
        db.query(ChannelOwnership)
        .filter(
            ChannelOwnership.user_id == user_id,
            ChannelOwnership.channel_id == channel_id,
        )
        .one_or_none()
    )
    if own is None:
        own = ChannelOwnership(
            user_id=user_id,
            channel_id=channel_id,
            google_user_id=google_user_id,
        )
        db.add(own)
        db.flush()
        return own
    if own.user_revoked_at is not None:
        # Sticky. Return the row as-is so the caller still gets a
        # ChannelOwnership object, but leave both revoke columns
        # alone - the ownership stays revoked until the user
        # re-authenticates.
        return own
    if own.revoked_at is not None:
        own.revoked_at = None
        db.flush()
    return own


def ensure_placeholder_video(
    db: Session,
    *,
    channel: Channel,
    youtube_video_id: str,
    title: Optional[str] = None,
    published_at: Optional[datetime] = None,
    privacy: Optional[str] = None,
) -> Video:
    """Ensure a pool row exists for a video we know about but have never
    captured.

    Until this existed, the only two things that created a Video row
    were a SUCCESSFUL sync and a PubSub notification about a public
    upload. A video that failed on every single attempt therefore had
    no pool row at all, and since every listing reads the pool, it was
    invisible everywhere in the UI - while still being counted by the
    home page's failure banner, which counts jobs rather than videos.
    That is how "3 videos failed to back up" linked to a list of one.

    Visibility is stamped from the privacy we know, and defaults to
    ``sealed`` when we know nothing. Sealed is owner-only, so guessing
    wrong here withholds a video rather than exposing one. The guess is
    also not permanent: a row that has never been captured is a
    placeholder, not a capture, so record_synced_video re-stamps it on
    the first real sync. "Frozen at capture" only starts meaning
    something once there is a capture.
    """
    video = (
        db.query(Video)
        .filter(Video.youtube_id == youtube_video_id)
        .one_or_none()
    )
    if video is not None:
        return video

    effective = privacy or "private"
    video = Video(
        channel_id=channel.id,
        youtube_id=youtube_video_id,
        title=title or youtube_video_id,
        published_at=published_at or datetime.now(timezone.utc),
        privacy_at_discovery=effective,
        privacy_current=effective,
        visibility=visibility_for_privacy(effective),
        r2_key=None,
        bytes_stored=None,
    )
    db.add(video)
    db.flush()
    return video


def record_synced_video(
    db: Session,
    *,
    user_id: str,
    youtube_channel_id: str,
    youtube_video_id: str,
    channel_title: Optional[str] = None,
    channel_handle: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    thumbnail_url: Optional[str] = None,
    published_at: Optional[datetime] = None,
    duration_seconds: Optional[int] = None,
    privacy: str = "public",
    r2_key: Optional[str] = None,
    bytes_stored: Optional[int] = None,
    google_user_id: Optional[str] = None,
    metadata_json: Optional[str] = None,
) -> Video:
    """Single entry point for "worker reported a successfully-synced
    video." Cascades the new-model writes:

      1. Channel for ``youtube_channel_id`` (fresh or existing)
      2. Subscription for (user, channel) — reactivate if soft-deleted
      3. ChannelOwnership for (user, channel) when a google_user_id
         is provided — preserves the legacy "they had access via the
         worker" semantics
      4. Video for ``youtube_video_id`` — created fresh, or
         updated in place with the latest privacy / r2_key /
         bytes_stored / synced_at when it already exists

    Returns the Video.
    """
    now = datetime.now(timezone.utc)

    channel = ensure_channel(
        db,
        youtube_channel_id,
        title=channel_title,
        handle=channel_handle,
    )
    ensure_subscription(db, user_id, channel.id)
    if google_user_id:
        ensure_ownership(
            db,
            user_id,
            channel.id,
            google_user_id=google_user_id,
        )

    video = (
        db.query(Video)
        .filter(Video.youtube_id == youtube_video_id)
        .one_or_none()
    )
    if video is None:
        video = Video(
            channel_id=channel.id,
            youtube_id=youtube_video_id,
            title=title or youtube_video_id,
            description=description,
            thumbnail_url=thumbnail_url,
            published_at=published_at or now,
            duration_seconds=duration_seconds,
            privacy_at_discovery=privacy,
            privacy_current=privacy,
            visibility=visibility_for_privacy(privacy),
            r2_key=r2_key,
            bytes_stored=bytes_stored,
            synced_at=now if r2_key else None,
            metadata_json=metadata_json,
        )
        db.add(video)
        db.flush()
        return video

    # Existing row — refresh fields. privacy_at_discovery stays put
    # (it's the snapshot), but privacy_current tracks YouTube's
    # latest state. r2_key + bytes_stored + synced_at update when
    # the caller has new sync data.
    if title and not video.title:
        video.title = title
    if description and not video.description:
        video.description = description
    if thumbnail_url:
        video.thumbnail_url = thumbnail_url
    if published_at:
        video.published_at = published_at
    if duration_seconds is not None:
        video.duration_seconds = duration_seconds
    video.privacy_current = privacy
    if video.r2_key is None and r2_key:
        # First real capture of what was until now a placeholder. The
        # freeze rule protects a visibility decided AT capture; this row
        # never had one, so stamp it properly now rather than leaving a
        # conservative guess to withhold a public video forever.
        video.privacy_at_discovery = privacy
        video.visibility = visibility_for_privacy(privacy)
    if r2_key:
        video.r2_key = r2_key
    if bytes_stored is not None:
        video.bytes_stored = bytes_stored
    if r2_key and video.synced_at is None:
        video.synced_at = now
    if metadata_json is not None:
        video.metadata_json = metadata_json
    db.flush()
    return video


# ============================================================
# Response-shape helpers
# ============================================================


def _safe_loads(s: Optional[str]) -> Dict[str, Any]:
    if not s:
        return {}
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        return {}


def channel_billable_bytes(db: Session, channel: Channel, user) -> int:
    """The bytes this user is billed for on this channel, right now.

    Sums Video.bytes_stored (video file + its thumbnail) across every
    video the user has access to - the same basis billing meters. The
    header's Storage stat shows THIS number so that the arithmetic a
    user naturally does (storage x advertised rate = cost) holds on
    screen; summing raw video-file sizes client-side made Storage a few
    percent smaller than the billed bytes and the numbers never quite
    reconciled.
    """
    # Local import to keep app.billing out of this module's load-
    # time graph (it pulls Stripe SDK, which is heavy).
    from app import billing as billing_lib

    # Bytes the user can see on this channel, scoped to one channel.
    # Open videos count toward an active subscriber; sealed toward the
    # authenticated owner. Visibility is frozen at capture, so this no
    # longer shifts when YouTube's privacy changes.
    from app.access import has_active_subscription, is_channel_owner

    total_bytes = 0

    if has_active_subscription(db, user.id, channel.id):
        rows = (
            db.query(Video.bytes_stored)
            .filter(
                Video.channel_id == channel.id,
                Video.bytes_stored.is_not(None),
                Video.bytes_stored > 0,
                Video.visibility == "open",
            )
            .all()
        )
        total_bytes += sum(b or 0 for (b,) in rows)
    # A user who revoked the worker's authentication stopped being metered
    # for sealed bytes at that instant (billing.py cuts the sealed window at
    # revoked_at). is_channel_owner() still answers True for 30 days of
    # grace, so leaning on it alone would keep quoting a private-video cost
    # the user is no longer being charged - on the very card that just told
    # them the revoke took effect. Only the user's own revocation is checked
    # here; the machine-side revoked_at grace is left as it was.
    user_revoked = (
        db.query(ChannelOwnership.id)
        .filter(
            ChannelOwnership.user_id == user.id,
            ChannelOwnership.channel_id == channel.id,
            ChannelOwnership.user_revoked_at.is_not(None),
        )
        .first()
        is not None
    )
    if not user_revoked and is_channel_owner(db, user.id, channel.id):
        sealed_rows = (
            db.query(Video.bytes_stored)
            .filter(
                Video.channel_id == channel.id,
                Video.bytes_stored.is_not(None),
                Video.bytes_stored > 0,
                Video.visibility == "sealed",
            )
            .all()
        )
        total_bytes += sum(b or 0 for (b,) in sealed_rows)

    return total_bytes


def channel_projected_monthly_cost_usd(
    db: Session,
    channel: Channel,
    user,
    total_bytes: Optional[int] = None,
) -> float:
    """Estimate this user's monthly cost for keeping the channel
    subscribed at its current size: channel_billable_bytes projected out
    to one full month at their effective storage markup.

    Pure projection - doesn't account for grace-period leftovers,
    deletions over the month, new uploads that'll get archived, or the
    storage free tier. The number's intent is "at THIS moment, if I keep
    this subscription for a full month, what's the bill order of
    magnitude." Good enough for a transparency-display field; not for
    actual invoicing (the bill cron handles that with real byte-hour
    integration).

    ``total_bytes`` lets a caller that already computed the billable
    bytes (to display them) avoid a second pass; cost and Storage then
    provably describe the same bytes.
    """
    from app import billing as billing_lib

    if total_bytes is None:
        total_bytes = channel_billable_bytes(db, channel, user)
    if total_bytes <= 0:
        return 0.0

    markup = billing_lib.get_user_storage_markup(user)
    byte_hours = total_bytes * billing_lib.HOURS_PER_MONTH_AVG
    return billing_lib.byte_hours_to_user_charge_usd(byte_hours, markup)


# Avatar-key existence cache. Presigning is blind: a key that no longer
# resolves still produces a perfectly well-formed URL, so the browser just
# gets a 403 and shows an empty circle with nothing logged anywhere. That is
# exactly how the shared-pool Channel rows sat pointing at the dead legacy
# "avatars/{channel_id}.jpg" prefix without anybody noticing.
#
# A HeadObject before every presign would fix the silence but add a storage
# round trip (and a billed Class B op) per channel per list request, so the
# answer is memoized in-process instead. Avatar keys only move when a rescan
# rotates the image, so a long TTL on "present" is safe; "missing" gets a
# short TTL so a repaired pointer starts working again without a restart.
_AVATAR_EXISTS_CACHE: Dict[str, Tuple[bool, float]] = {}
_AVATAR_PRESENT_TTL_SECONDS = 3600.0
_AVATAR_MISSING_TTL_SECONDS = 300.0


def _avatar_object_exists(key: str, subject: str) -> bool:
    """True when ``key`` actually resolves in storage, memoized per key.

    Errors other than a clean 404 (storage not configured, a transient
    network fault) return True so we keep the old behavior and presign
    anyway - a working avatar must never disappear because a HEAD blipped.
    """
    now = time.monotonic()
    cached = _AVATAR_EXISTS_CACHE.get(key)
    if cached is not None and cached[1] > now:
        return cached[0]

    from app import r2  # local import to avoid circular at module load

    try:
        exists = r2.head(key, subject=subject) is not None
    except Exception:  # noqa: BLE001
        return True
    ttl = _AVATAR_PRESENT_TTL_SECONDS if exists else _AVATAR_MISSING_TTL_SECONDS
    # One entry per channel, so this is small by construction. The cap is only
    # here so a pathological key churn can't grow it without bound; dropping
    # everything just costs one HEAD per channel to refill.
    if len(_AVATAR_EXISTS_CACHE) > 10_000:
        _AVATAR_EXISTS_CACHE.clear()
    _AVATAR_EXISTS_CACHE[key] = (exists, now + ttl)
    return exists


def channel_response_payload(
    channel: Channel,
    subscription: UserChannelSubscription,
    *,
    projected_monthly_cost_usd: Optional[float] = None,
    billable_bytes: Optional[int] = None,
    archived_video_count: Optional[int] = None,
    known_video_count: Optional[int] = None,
    comments_sync_available: Optional[bool] = None,
) -> Dict[str, Any]:
    """Assemble the frontend's Channel-shaped payload from a Channel +
    (optional) UserChannelSubscription row pair. Used by GET
    /api/youtube/channels and /channels/{id}.

    Composes:
      - structured Channel columns (id, title, handle, thumbnail_url)
      - Channel.metadata_json (YouTube-side rich info)
      - Subscription.settings_json + .subscribed_at + .last_synced_at
        (per-user state)
      - optional projected_monthly_cost_usd, surfaced on the card as
        a transparency hint (caller decides whether to compute it -
        we don't run the per-channel sum on every payload because
        the channel-detail page doesn't need it)

    When channel.avatar_r2_key is set AND the object is really there,
    swaps the YouTube CDN URL for a presigned storage URL so the frontend
    never touches YouTube's CDN. A key that no longer resolves falls back
    to the CDN url instead of returning a URL that 403s.
    """
    meta = _safe_loads(channel.metadata_json)
    sub_settings = _safe_loads(subscription.settings_json)
    avatar_url = channel.thumbnail_url or meta.get("avatarUrl") or ""
    if channel.avatar_r2_key:
        try:
            from app import r2  # local import to avoid circular at module load

            if _avatar_object_exists(channel.avatar_r2_key, subscription.user_id):
                avatar_url = r2.presign_get(
                    channel.avatar_r2_key,
                    expires_in=3600,
                    subject=subscription.user_id,
                )
            else:
                # Broken pointer: the row names a key that is not in the
                # bucket. Keep whatever thumbnail_url/metadata held rather
                # than handing the browser a URL we know 403s, and say so
                # loudly enough to grep for next time. Note the fallback is
                # not guaranteed good either - rows poisoned before the
                # write-side gate landed hold an expired signature there
                # too. scripts/backfill_avatar_mirror.py is the real repair.
                log.warning(
                    "avatar object missing in storage for channel %s (%s): "
                    "key=%s - falling back to the stored thumbnail url",
                    channel.youtube_id,
                    channel.title or "",
                    channel.avatar_r2_key,
                )
        except Exception:  # noqa: BLE001
            pass  # fall back to whatever we had
    # PubSub liveness signal: if our hub lease is current, we're
    # receiving upload notifications within seconds of publish. If
    # expired or never set, we're falling back to polling/manual sync.
    # Surfaces a "Live" badge on the frontend so users can see the
    # discovery quality at a glance.
    pubsub_live = False
    if channel.pubsub_lease_expires_at is not None:
        lease = channel.pubsub_lease_expires_at
        if lease.tzinfo is None:
            from datetime import timezone as _tz

            lease = lease.replace(tzinfo=_tz.utc)
        pubsub_live = lease > datetime.now(timezone.utc)
    payload: Dict[str, Any] = {
        # Structured Channel columns - explicit > metadata_json wins
        "id": channel.youtube_id,
        "handle": channel.handle or meta.get("handle") or channel.youtube_id,
        "name": channel.title or meta.get("name") or channel.youtube_id,
        "avatarUrl": avatar_url,
        # YouTube-side info from metadata_json
        "description": meta.get("description", ""),
        "subscriberCount": meta.get("subscriberCount", 0),
        "videoCount": meta.get("videoCount", 0),
        "totalViews": meta.get("totalViews", 0),
        "country": meta.get("country", ""),
        "joinedAt": meta.get("joinedAt", ""),
        "links": meta.get("links", []),
        "terminatedAt": meta.get("terminatedAt"),
        "youtubeStatus": meta.get("youtubeStatus", "available"),
        # Per-user state from subscription
        "addedAt": (
            subscription.subscribed_at.isoformat()
            if subscription.subscribed_at
            else ""
        ),
        "lastSyncedAt": (
            subscription.last_synced_at.isoformat()
            if subscription.last_synced_at
            else ""
        ),
        "settings": sub_settings,
        "pubsubLive": pubsub_live,
        # Real count of this channel's videos we've actually archived
        # (downloaded to storage). The card shows
        # "<archivedVideoCount> / <knownVideoCount>".
        "archivedVideoCount": archived_video_count or 0,
        # Videos WE know about and this caller may see - the denominator
        # of that ratio, and deliberately not videoCount.
        #
        # videoCount is YouTube's public number, which counts only what a
        # stranger can see. Archiving private videos is the entire point
        # of authenticating a channel, so the moment it works the
        # numerator counts videos the denominator never could. The owner
        # watched his own card read "11 / 9": eleven archived against nine
        # videos YouTube admits exist. Both numbers were right; they were
        # answers to different questions.
        #
        # Falls back to videoCount only when the caller did not compute
        # it, so an un-updated call site degrades to the old behaviour
        # rather than rendering "11 / 0".
        "knownVideoCount": (
            known_video_count
            if known_video_count is not None
            else meta.get("videoCount", 0)
        ),
        # Comment syncing goes through the YouTube Data API with the
        # channel's OAuth credentials, so it's only possible on channels
        # imported from a connected account - a URL-tracked channel has no
        # token and the cron skips it. Surfaced so the UI can disable the
        # toggle instead of letting it silently do nothing forever.
        "commentsSyncAvailable": bool(comments_sync_available),
    }
    if projected_monthly_cost_usd is not None:
        payload["projectedMonthlyCostUsd"] = round(
            projected_monthly_cost_usd, 4
        )
    # The bytes the cost above was computed FROM, so the frontend can show
    # a Storage figure whose product with the advertised rate is the Cost
    # beside it. Client-side sums of video-file sizes undercount (they miss
    # thumbnail bytes) and made the two stats look unrelated.
    if billable_bytes is not None:
        payload["bytesStored"] = int(billable_bytes)
    return payload


_PER_USER_METADATA_KEYS = ("localPath", "r2Key", "r2_key")


def video_response_payload(video: Video) -> Dict[str, Any]:
    """Assemble the frontend's video-row payload from a Video row.
    Uses Video.metadata_json for the rich fields (viewCount, tags,
    captionLanguages, videoResolution, etc.) and overlays the
    authoritative structured columns on top so e.g. privacy_current
    always wins over a stale metadata_json snapshot.
    """
    meta = _safe_loads(video.metadata_json)
    # Start from the legacy data_json so we keep arbitrary frontend
    # fields we don't model explicitly.
    payload = dict(meta)
    # ...except the keys that belong to whoever ARCHIVED it rather than to
    # the video. metadata_json is copied wholesale from the archiving
    # user's own row, and localPath is a storage key of the form
    # users/<their-uuid>/videos/... - so returning it hands every other
    # subscriber of a shared channel another tenant's internal user id.
    # It also makes the UI lie: the frontend treats a non-null localPath
    # as "we have this file", which was true for them and not for the
    # caller. Callers that legitimately have their own copy get it back
    # from their per-user row in the route's overlay.
    for _k in _PER_USER_METADATA_KEYS:
        payload.pop(_k, None)
    payload.update(
        {
            "id": video.youtube_id,
            "title": video.title,
            "description": video.description or meta.get("description", ""),
            "thumbnailUrl": video.thumbnail_url
            or meta.get("thumbnailUrl", ""),
            "durationSec": video.duration_seconds
            or meta.get("durationSec", 0),
            "uploadDate": (
                video.published_at.isoformat()
                if video.published_at
                else meta.get("uploadDate", "")
            ),
            "privacy": video.privacy_current,
            "visibility": video.visibility,
            "fileSizeBytes": video.bytes_stored or 0,
            "archivedAt": (
                video.synced_at.isoformat() if video.synced_at else None
            ),
            "status": "archived" if video.r2_key else "discovered",
        }
    )
    return payload
