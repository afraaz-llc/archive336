"""Single source of all writes to the ``storage_objects`` table.

Every R2 PUT site in the codebase calls :func:`record_object` after a
successful upload; every R2 DELETE site calls :func:`mark_deleted`
after a successful delete; soft-delete propagation for channels goes
through :func:`propagate_channel_soft_delete` / :func:`propagate_channel_restore`.

See ``docs/STORAGE_BILLING_DESIGN.md`` for the full design.

Ordering rules:
- Insert AFTER the R2 PUT returns 200. Never before.
- For DELETE: issue the R2 DELETE first, THEN call mark_deleted. If
  R2 DELETE fails, do NOT call mark_deleted (we're still being billed
  by R2; leave the ledger row open). If R2 DELETE succeeds but
  mark_deleted fails, reconciliation will catch the orphan within
  24h and flip deleted_at retroactively.

Session lifecycle:
- All helpers stage on the caller's session and let the caller commit
  explicitly. They never commit on their own (matches the route-handler
  pattern elsewhere in the codebase).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Iterable, List, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import StorageObject, UserChannel, UserChannelVideo


log = logging.getLogger("archive336.storage_ledger")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def record_object(
    db: Session,
    *,
    user_id: str,
    r2_key: str,
    byte_count: int,
    kind: str,
    uploaded_at: Optional[datetime] = None,
    metadata_bytes: Optional[int] = None,
) -> Optional[StorageObject]:
    """Insert a StorageObject row for an R2 object we just uploaded.

    Call this AFTER the R2 PUT returns 200. Never before.

    ``metadata_bytes`` should be the value returned by the r2.upload_file
    or r2.download_to_r2 helper that wrote the object — they compute it
    from the content_type and any custom headers they sent. When omitted
    (None), the StorageObject column default (256 bytes) kicks in, which
    is a deliberate over-estimate kept for callers we haven't migrated
    yet. The reconciliation cron will backfill these on its next walk.

    Behavior when an ACTIVE row (``deleted_at IS NULL``) with this
    ``r2_key`` already exists:

      - **Same byte_count + same metadata_bytes** → idempotent no-op,
        returns None. Covers safe retries of an unchanged PUT.

      - **Different byte_count or metadata_bytes** → the existing row's
        lifecycle ends NOW (``deleted_at`` set to ``uploaded_at``), a
        fresh row opens with the new bytes. Preserves the historical
        byte-hours for the old content size so CF reconciliation
        matches. Returns the new row.

    This matters because R2 doesn't require a DELETE before an overwrite
    PUT — boto3's ``upload_file`` will happily replace an object in
    place — and we used to skip the new-row insert in that case,
    silently keeping a stale size in the ledger. CF caught it on
    every monthly invoice; we just couldn't see the drift until the
    reconciliation cron landed.

    Concurrent inserts remain race-safe via the partial UNIQUE index
    on (r2_key) WHERE deleted_at IS NULL — the loser gets an
    IntegrityError which we swallow.

    Returns the inserted StorageObject, or None if no new row was
    needed (idempotent case).
    """
    if byte_count <= 0:
        log.warning(
            "record_object skipping %s — non-positive byte count %d",
            r2_key,
            byte_count,
        )
        return None

    when = uploaded_at or _now()

    # Look up any active row at this key. If sizes match the incoming
    # PUT, the call is idempotent. If sizes differ, the content changed
    # under us — close out the old lifecycle so CF byte-hours line up.
    existing = (
        db.query(StorageObject)
        .filter(
            StorageObject.r2_key == r2_key,
            StorageObject.deleted_at.is_(None),
        )
        .first()
    )
    if existing is not None:
        same_size = existing.bytes == byte_count and (
            metadata_bytes is None
            or existing.metadata_bytes == metadata_bytes
        )
        if same_size:
            log.info(
                "record_object: active row matches at %s, skipping (idempotent)",
                r2_key,
            )
            return None
        log.info(
            "record_object: overwrite at %s, closing old row "
            "(bytes %d→%d, meta %d→%s) and opening new",
            r2_key,
            existing.bytes,
            byte_count,
            existing.metadata_bytes,
            metadata_bytes if metadata_bytes is not None else "default",
        )
        existing.deleted_at = when
        # Flush the close-out before inserting so the partial UNIQUE
        # index doesn't complain about two active rows at the same key.
        db.flush()

    obj_kwargs = dict(
        user_id=user_id,
        r2_key=r2_key,
        bytes=byte_count,
        kind=kind,
        uploaded_at=when,
    )
    if metadata_bytes is not None:
        obj_kwargs["metadata_bytes"] = metadata_bytes
    obj = StorageObject(**obj_kwargs)
    db.add(obj)
    # Flush so the partial UNIQUE index fires now rather than at commit
    # (when we can't recover gracefully). Concurrent insert? Treat as
    # "someone beat us to it" and continue.
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        log.warning(
            "record_object: UNIQUE race on %s — concurrent insert won",
            r2_key,
        )
        return None
    return obj


def mark_deleted(
    db: Session,
    r2_keys: Iterable[str],
    *,
    deleted_at: Optional[datetime] = None,
) -> int:
    """Flip ``deleted_at`` for each r2_key that's still active.

    Call this AFTER the R2 DELETE returns 200. If R2 DELETE failed,
    do NOT call this — leave the row open (we're still being billed
    by R2). Reconciliation will detect drift in either direction.

    Idempotent: skips rows that already have deleted_at set; will not
    overwrite the original delete timestamp on re-runs.

    Returns the number of rows updated.
    """
    keys_list = [k for k in r2_keys if k]
    if not keys_list:
        return 0
    when = deleted_at or _now()
    return (
        db.query(StorageObject)
        .filter(
            StorageObject.r2_key.in_(keys_list),
            StorageObject.deleted_at.is_(None),
        )
        .update({"deleted_at": when}, synchronize_session=False)
    )


def rotate_in_place(
    db: Session,
    *,
    user_id: str,
    r2_key: str,
    new_history_key: str,
    new_bytes: int,
    kind: str,
    history_kind: str = "snapshot",
    rotated_at: Optional[datetime] = None,
    new_metadata_bytes: Optional[int] = None,
    keep_history: bool = True,
) -> Optional[StorageObject]:
    """Record that the R2 content at ``r2_key`` was overwritten with new
    bytes (size ``new_bytes``). When ``keep_history`` is True the old bytes
    were also copied to ``new_history_key`` (still in R2, now at a versioned
    path) and get their own ledger row; when False the old content is simply
    superseded (the caller didn't preserve the old bytes in R2).

    Ledger ops, in order:
      1. Look up the existing active row at r2_key. If it exists and
         ``keep_history``, insert a NEW row at ``new_history_key`` with the
         *old* bytes (preserving the old size for billing continuity).
      2. Mark the existing row at r2_key deleted (its lifecycle at
         this key is over — the new content is a separate lifecycle).
      3. Insert a fresh active row at r2_key with the new bytes.

    Returns the new active row at r2_key (the "current" representation
    of what R2 now serves at this key), or None if both R2 ops failed
    and there's nothing to record.
    """
    when = rotated_at or _now()
    existing = (
        db.query(StorageObject)
        .filter(
            StorageObject.r2_key == r2_key,
            StorageObject.deleted_at.is_(None),
        )
        .first()
    )
    if existing is not None:
        if keep_history:
            # Preserve the old bytes under the history key. This row
            # tracks the OLD content's life going forward at the new
            # location. Metadata size carries over from the original
            # since R2 copy operations preserve user metadata.
            history_row = StorageObject(
                user_id=user_id,
                r2_key=new_history_key,
                bytes=existing.bytes,
                metadata_bytes=existing.metadata_bytes,
                kind=history_kind,
                uploaded_at=when,
            )
            db.add(history_row)
        # Close out the existing row's lifecycle at the canonical key.
        existing.deleted_at = when
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            log.warning(
                "rotate_in_place: history_key %s already had an active row",
                new_history_key,
            )

    return record_object(
        db,
        user_id=user_id,
        r2_key=r2_key,
        byte_count=new_bytes,
        kind=kind,
        uploaded_at=when,
        metadata_bytes=new_metadata_bytes,
    )


def keys_from_video_data(data: dict) -> List[str]:
    """Every storage key implied by one video's data_json blob.

    The single definition of "which objects belong to this video". Three
    call sites used to hand-roll this, they drifted, and two of the three
    silently stopped deleting anything: each tested only for the legacy
    `videos/...` prefix while uploads had moved to
    `users/{uid}/videos/...`. One was the channel purge (852 MB of video
    left behind), the other was account deletion (every mp4 and caption
    surviving the deletion the user asked for). Both now call this.

    Accepts either layout. Anything else is a desktop filesystem path
    from pre-MVP data and is skipped rather than handed to a delete call.
    """
    keys: List[str] = []
    local_path = (data.get("localPath") or "").strip()
    if not (
        local_path.startswith("videos/") or local_path.startswith("users/")
    ):
        return keys
    keys.append(local_path)
    base = (
        local_path.rsplit("/video.mp4", 1)[0]
        if local_path.endswith("/video.mp4")
        else None
    )
    langs = data.get("captionLanguages") or []
    if base and isinstance(langs, list):
        for lang in langs:
            if isinstance(lang, str) and lang:
                keys.append(f"{base}/captions/{lang}.vtt")
    return keys


def channel_r2_keys(db: Session, user_id: str, channel_id: str) -> List[str]:
    """Enumerate every R2 key tied to this channel: avatar +
    per-video thumbnails + per-video mp4 (via data_json.localPath) +
    per-video captions (via data_json.captionLanguages).

    THE single definition of "this channel's objects", used by both the
    soft-delete that marks them and the purge that erases them. It used to
    be duplicated in scripts.purge_removed with a docstring claiming the two
    mirrored each other - they drifted. This copy learned the per-user key
    prefix (users/<uid>/videos/...) and captions; the purge copy never did,
    so purge silently skipped every video file and left the largest objects
    in the bucket while telling the user the archive was deleted. One
    function now, so they cannot disagree again.
    """
    keys: List[str] = []
    ch = db.get(UserChannel, (user_id, channel_id))
    if ch and ch.avatar_r2_key:
        keys.append(ch.avatar_r2_key)

    videos = (
        db.query(UserChannelVideo)
        .filter(
            UserChannelVideo.user_id == user_id,
            UserChannelVideo.channel_id == channel_id,
        )
        .all()
    )
    for v in videos:
        if v.thumbnail_r2_key:
            keys.append(v.thumbnail_r2_key)
        try:
            data = json.loads(v.data_json)
        except (json.JSONDecodeError, TypeError):
            continue
        keys.extend(keys_from_video_data(data))
    return keys


def propagate_channel_soft_delete(
    db: Session,
    user_id: str,
    channel_id: str,
    *,
    removed_at: datetime,
) -> int:
    """When a channel is soft-deleted, mark all its StorageObjects
    deleted at the same time.

    This matches the existing meter behavior (don't bill during the
    30-day grace window). The R2 objects stay in place until the
    daily purge cron actually drops them; we eat that cost during
    the grace window as a deliberate UX choice.

    Returns the count of rows updated. Idempotent.
    """
    keys = channel_r2_keys(db, user_id, channel_id)
    return mark_deleted(db, keys, deleted_at=removed_at)


def propagate_channel_restore(
    db: Session,
    user_id: str,
    channel_id: str,
) -> int:
    """When a soft-deleted channel is restored (re-imported within
    the grace window), clear ``deleted_at`` on its StorageObjects so
    billing resumes from now.

    Returns the count of rows updated.
    """
    keys = channel_r2_keys(db, user_id, channel_id)
    if not keys:
        return 0
    return (
        db.query(StorageObject)
        .filter(
            StorageObject.user_id == user_id,
            StorageObject.r2_key.in_(keys),
            StorageObject.deleted_at.is_not(None),
        )
        .update({"deleted_at": None}, synchronize_session=False)
    )
