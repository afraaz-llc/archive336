"""Channel-level info refresh + history.

The channel analogue of ``metadata_rescan`` (which handles per-video fields).
Called once per channel per run by the metadata rescan cron, on the channel's
metadata cadence. Re-fetches the channel's public info and, per the per-field
save/history toggles:

  - **about**  : if the channel description changed, keep the old text as a
                 ChannelFieldSnapshot (history), then update the current value.
  - **avatar** : byte-hash the profile picture; if it changed, copy the old
                 image to a versioned history key + snapshot it, then write the
                 new bytes to the canonical avatar key.
  - **stats**  : append a point-sample (subscriber/video/view counts) every
                 refresh when history is on — a time-series graph over time.

All best-effort: a failure on one field is logged and never blocks the others
or the video rescan. The current values live in ``UserChannel.data_json``;
snapshots live in ``channel_field_snapshots``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import requests
from sqlalchemy.orm import Session

from app import r2, r2_paths, storage_ledger
from app.models import Channel, ChannelFieldSnapshot, UserChannel
from app.youtube_scrape import (
    fetch_channel_about,
    fetch_channel_avatar_url,
    fetch_channel_stats,
)

log = logging.getLogger("aether.channel_rescan")


def _parse_iso(s: Any) -> Optional[datetime]:
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _snapshot(
    db: Session,
    *,
    user_id: str,
    channel_id: str,
    field: str,
    value: Any,
    r2_key: Optional[str],
    captured_at: datetime,
    superseded_at: datetime,
) -> None:
    db.add(
        ChannelFieldSnapshot(
            user_id=user_id,
            channel_id=channel_id,
            field=field,
            value_json=json.dumps(value),
            r2_key=r2_key,
            captured_at=captured_at,
            superseded_at=superseded_at,
        )
    )


def _due(data: Dict[str, Any], cadence_days: int, now: datetime) -> bool:
    """Channel info refreshes on the same cadence as the video rescan.
    Due if never refreshed or the last refresh is older than the window."""
    last = _parse_iso(data.get("lastChannelInfoSyncAt"))
    if last is None:
        return True
    return (now - last) >= timedelta(days=cadence_days)


def refresh_channel_info(
    db: Session,
    *,
    user_channel: UserChannel,
    settings: Dict[str, Any],
    cadence_days: int,
    now: datetime,
) -> Dict[str, bool]:
    """Refresh about/avatar/stats for one channel when due. Mutates
    ``user_channel.data_json`` (current values) and adds ChannelFieldSnapshot
    rows (history). Caller commits. Returns which fields changed."""
    try:
        data = json.loads(user_channel.data_json)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not _due(data, cadence_days, now):
        return {}

    uid = user_channel.user_id
    cid = user_channel.channel_id
    # captured_at for a superseded value = the prior refresh (or add time).
    prev_at = (
        _parse_iso(data.get("lastChannelInfoSyncAt"))
        or _parse_iso(data.get("addedAt"))
        or now
    )
    changed: Dict[str, bool] = {}
    # Did ANY public probe return data this run? Drives termination detection
    # below - all-empty twice in a row means the channel is gone.
    saw_data = False

    # ---- About (channel description) ----
    if settings.get("saveChannelAbout", True):
        try:
            about = fetch_channel_about(cid)
        except Exception:  # noqa: BLE001
            about = None
        if about is not None:
            saw_data = True
            new_desc = about.get("description") or ""
            old_desc = data.get("description") or ""
            if new_desc != old_desc:
                if old_desc and settings.get("saveChannelAboutHistory", True):
                    _snapshot(
                        db, user_id=uid, channel_id=cid, field="about",
                        value=old_desc, r2_key=None,
                        captured_at=prev_at, superseded_at=now,
                    )
                data["description"] = new_desc
                changed["about"] = True

    # ---- Stats (time-series) ----
    if settings.get("saveChannelStatsSnapshots", True):
        try:
            stats = fetch_channel_stats(cid)
        except Exception:  # noqa: BLE001
            stats = None
        if stats is not None:
            saw_data = True
            if settings.get("saveChannelStatsHistory", True):
                _snapshot(
                    db, user_id=uid, channel_id=cid, field="stats",
                    value={
                        "subscriberCount": stats.get("subscriberCount"),
                        "videoCount": stats.get("videoCount"),
                        "totalViews": stats.get("totalViews"),
                    },
                    r2_key=None, captured_at=now, superseded_at=now,
                )
            data["subscriberCount"] = stats.get("subscriberCount")
            data["videoCount"] = stats.get("videoCount")
            data["totalViews"] = stats.get("totalViews")
            changed["stats"] = True

    # ---- Avatar (byte-hash change detection + history rotation) ----
    if settings.get("saveChannelAvatar", True):
        try:
            _refresh_avatar(
                db, user_channel=user_channel, data=data,
                settings=settings, prev_at=prev_at, now=now, changed=changed,
            )
        except Exception:  # noqa: BLE001
            log.exception("avatar refresh failed for %s/%s", uid, cid)

    # ---- Termination detection ----
    # If every public probe came back empty the channel is very likely
    # terminated/removed. One failed fetch is NOT enough (a network blip
    # would cry wolf), so this needs two consecutive whiffs before declaring
    # it. Any successful probe resets the counter.
    # Only meaningful if we actually probed something this run - with both
    # About and Stats capture off we have no signal and must not guess.
    probed = bool(
        settings.get("saveChannelAbout", True)
        or settings.get("saveChannelStatsSnapshots", True)
    )
    if probed:
        _apply_termination_signal(
            db,
            user_channel=user_channel,
            data=data,
            settings=settings,
            saw_data=saw_data,
            now=now,
            changed=changed,
        )

    data["lastChannelInfoSyncAt"] = now.isoformat()
    user_channel.data_json = json.dumps(data)
    return changed


def _apply_termination_signal(
    db: Session,
    *,
    user_channel: UserChannel,
    data: Dict[str, Any],
    settings: Dict[str, Any],
    saw_data: bool,
    now: datetime,
    changed: Dict[str, bool],
) -> None:
    """Track consecutive 'channel returned nothing' refreshes and, on the
    second one, mark the channel terminated + fire the integrity alert."""
    if saw_data:
        if data.get("channelInfoFailures"):
            data["channelInfoFailures"] = 0
        # A channel that answers again is not terminated any more.
        if data.get("youtubeStatus") == "terminated":
            data["youtubeStatus"] = "available"
            data["terminatedAt"] = None
        return

    failures = int(data.get("channelInfoFailures") or 0) + 1
    data["channelInfoFailures"] = failures
    if failures < 2 or data.get("youtubeStatus") == "terminated":
        return

    data["youtubeStatus"] = "terminated"
    data["terminatedAt"] = now.isoformat()
    changed["terminated"] = True
    try:
        from app import notify as notify_lib  # noqa: WPS433

        notify_lib.notify_channel_terminated(
            db,
            user_id=user_channel.user_id,
            channel_youtube_id=user_channel.channel_id,
            channel_name=data.get("name") or user_channel.channel_id,
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "channel-terminated notification failed for %s/%s",
            user_channel.user_id, user_channel.channel_id,
        )


def _read_r2_bytes(key: str, subject: str) -> Optional[bytes]:
    client = r2.client()
    bucket = r2.bucket()
    if not (client and bucket):
        return None
    try:
        obj = client.get_object(Bucket=bucket, Key=key)
        return obj["Body"].read()
    except Exception:  # noqa: BLE001
        return None


def _refresh_avatar(
    db: Session,
    *,
    user_channel: UserChannel,
    data: Dict[str, Any],
    settings: Dict[str, Any],
    prev_at: datetime,
    now: datetime,
    changed: Dict[str, bool],
) -> None:
    uid = user_channel.user_id
    cid = user_channel.channel_id
    new_url = fetch_channel_avatar_url(cid)
    if not new_url or "picsum.photos" in new_url:
        return

    try:
        resp = requests.get(new_url, timeout=10)
        resp.raise_for_status()
    except requests.RequestException:
        return
    new_bytes = resp.content
    new_sha = hashlib.sha256(new_bytes).hexdigest()

    old_sha = data.get("avatarSha")
    if old_sha is None and user_channel.avatar_r2_key:
        existing = _read_r2_bytes(user_channel.avatar_r2_key, uid)
        if existing is not None:
            old_sha = hashlib.sha256(existing).hexdigest()

    if old_sha == new_sha:
        # Unchanged — persist the baseline hash so we can short-circuit next
        # time without re-reading R2. (URL may cache-bust; bytes are equal.)
        data["avatarSha"] = new_sha
        return

    canonical = r2_paths.avatar_key(uid, cid)
    client = r2.client()
    bucket = r2.bucket()
    if not (client and bucket):
        return

    # Preserve the old image as history first, when we have one and the
    # history toggle is on.
    keep_history = (
        bool(old_sha)
        and bool(user_channel.avatar_r2_key)
        and settings.get("saveChannelAvatarHistory", True)
    )
    if keep_history:
        ts = now.strftime("%Y%m%dT%H%M%SZ")
        hist_key = r2_paths.avatar_history_key(uid, cid, ts)
        try:
            client.copy_object(
                Bucket=bucket,
                Key=hist_key,
                CopySource={"Bucket": bucket, "Key": user_channel.avatar_r2_key},
            )
        except Exception:  # noqa: BLE001
            log.exception("avatar history copy failed for %s/%s", uid, cid)
        else:
            head = r2.head(user_channel.avatar_r2_key, subject=uid)
            if head is not None:
                storage_ledger.record_object(
                    db,
                    user_id=uid,
                    r2_key=hist_key,
                    byte_count=int(head.get("ContentLength") or 0),
                    kind="snapshot",
                    uploaded_at=now,
                    metadata_bytes=r2.metadata_bytes_for(
                        content_type=head.get("ContentType"),
                        custom_meta=head.get("Metadata"),
                    ),
                )
            _snapshot(
                db, user_id=uid, channel_id=cid, field="avatar",
                value={"url": data.get("avatarUrl"), "sha256": old_sha},
                r2_key=hist_key, captured_at=prev_at, superseded_at=now,
            )

    # Write the new bytes to the canonical key + update the ledger.
    try:
        client.put_object(
            Bucket=bucket,
            Key=canonical,
            Body=new_bytes,
            ContentType="image/jpeg",
        )
    except Exception:  # noqa: BLE001
        log.exception("avatar upload failed for %s/%s", uid, cid)
        return
    storage_ledger.rotate_in_place(
        db,
        user_id=uid,
        r2_key=canonical,
        new_history_key=canonical,
        new_bytes=len(new_bytes),
        kind="avatar",
        rotated_at=now,
        new_metadata_bytes=r2.metadata_bytes_for(content_type="image/jpeg"),
        keep_history=False,
    )
    user_channel.avatar_r2_key = canonical
    _mirror_avatar_key(db, channel_id=cid, key=canonical)
    data["avatarUrl"] = new_url
    data["avatarSha"] = new_sha
    changed["avatar"] = True


def _mirror_avatar_key(db: Session, *, channel_id: str, key: str) -> None:
    """Point the shared-pool Channel row at the avatar we just wrote.

    The read path (archive.channel_response_payload) presigns
    ``Channel.avatar_r2_key``, not ``UserChannel.avatar_r2_key``. Rotating
    the avatar here without mirroring leaves that column naming a key we no
    longer maintain - and a stale key presigns to a 403, which renders as a
    silently empty avatar with nothing in the logs. So the mirror is part of
    the write, not an optional extra.

    WRINKLE - per-user prefix on a channel-keyed row: ``canonical`` lives
    under ``users/{uid}/channels/{cid}/avatar.jpg`` because storage billing
    attributes every object to one owner. Channel is shared, so this row now
    points into one specific subscriber's prefix. That is deliberate and
    matches what routes/youtube.py already does on import; it is also what
    the frontend needs today. The failure mode is narrow but real: if that
    user's prefix is ever purged (account deletion, the removed-channel
    sweep) every other subscriber's avatar breaks at once. The proper fix is
    a channel-scoped copy at ``channels/{cid}/avatar.jpg`` owned by the
    platform, with this column pointing there and the per-user copy kept
    only for billing. Do that when purge actually starts deleting prefixes.
    Until then archive.py's existence check degrades a purged key to the CDN
    fallback instead of a broken image.
    """
    channel = (
        db.query(Channel).filter(Channel.youtube_id == channel_id).one_or_none()
    )
    # No shared-pool row yet (legacy-only channel, mid-migration) - the
    # UserChannel key above is still correct, so there is nothing to fix.
    if channel is None:
        return
    channel.avatar_r2_key = key
