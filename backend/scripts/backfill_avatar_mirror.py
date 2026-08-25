"""One-off repair: point Channel.avatar_r2_key back at bytes that exist.

Storage billing Phase C moved archived avatars to a per-user prefix
(users/{user_id}/channels/{channel_id}/avatar.jpg - see app/r2_paths.py).
UserChannel.avatar_r2_key followed the move; the shared-pool
Channel.avatar_r2_key mirror did not, so every channel added before
Phase C still mirrors the pre-Phase-C key avatars/{channel_id}.jpg.
That whole prefix is empty in the bucket now, and nothing self-heals
it: the sync route only fills the mirror when it is EMPTY, and
channel_rescan only ever writes the UserChannel side.

The read path (app/archive.py, via channel_response_payload) presigns
the Channel mirror, so a stale mirror hands the browser a perfectly
well-formed signed URL that answers 403. Empty circle, intact bytes.

This script repairs that one pointer column and nothing else. It never
deletes anything, in the DB or in storage, and it never writes to
storage. It HEADs both keys first and only moves the mirror when the
mirror's object is MISSING and the UserChannel's object is really
there, so a working pointer is never overwritten. Channels where
neither key resolves are reported loudly and left untouched - that is
a genuinely lost avatar and needs a refetch from YouTube, not a
pointer fix.

Idempotent: once a mirror points at a key with bytes behind it, later
runs skip it.

Usage:
    /opt/aether/venv/bin/python -m scripts.backfill_avatar_mirror --dry
    /opt/aether/venv/bin/python -m scripts.backfill_avatar_mirror --apply

Dry-run is the default - nothing is written unless --apply is passed.
Run from /opt/aether/app/backend (venv at /opt/aether/venv, env at
/opt/aether/.env).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Dict, List, Optional, Set

# Allow `python -m scripts.backfill_avatar_mirror` from /opt/aether/app/backend
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import ops_ledger, r2  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Channel, UserChannel  # noqa: E402


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


def _exists(key: str, cache: Dict[str, Optional[bool]]) -> Optional[bool]:
    """True/False when storage answered, None when the HEAD itself failed.

    Cached per key: a shared channel can have many subscriber rows
    pointing at the same object and every HEAD is a billed Class B op,
    so we ask once per distinct key. None is deliberately not folded
    into False - we never rewrite a pointer on the strength of an
    error, only on a real 404.
    """
    if key in cache:
        return cache[key]
    result: Optional[bool]
    try:
        result = r2.head(key, subject=ops_ledger.PLATFORM) is not None
    except Exception as e:
        log.warning("    ! HEAD failed for %s: %s", key, e)
        result = None
    cache[key] = result
    return result


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Repair stale Channel.avatar_r2_key mirrors."
    )
    parser.add_argument(
        "--dry",
        "--dry-run",
        dest="dry",
        action="store_true",
        help="Report only; write nothing. This is the default.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the repaired mirrors.",
    )
    args = parser.parse_args(argv)

    if args.dry and args.apply:
        log.warning("--dry and --apply both given; --dry wins, nothing will be written")
    write = args.apply and not args.dry

    inspected = 0
    in_sync = 0
    mirror_kept = 0
    repaired = 0
    borrowed = 0
    missing_everywhere = 0
    no_channel_row = 0
    skipped_error = 0

    db = SessionLocal()
    try:
        rows = (
            db.query(UserChannel)
            .filter(UserChannel.avatar_r2_key.isnot(None))
            .filter(UserChannel.avatar_r2_key != "")
            .all()
        )
        inspected = len(rows)
        log.info(
            "inspecting %d user_channels rows with an avatar key (mode=%s)",
            inspected,
            "apply" if write else "dry",
        )

        # The Channel mirror is shared, so group the subscriber rows by
        # YouTube channel id and decide once per channel. Soft-removed
        # subscribers sort last so a live subscriber's key always wins.
        by_channel: Dict[str, List[UserChannel]] = {}
        for row in rows:
            by_channel.setdefault(row.channel_id, []).append(row)
        for group in by_channel.values():
            group.sort(key=lambda r: r.removed_at is not None)

        exists_cache: Dict[str, Optional[bool]] = {}

        for channel_yt_id in sorted(by_channel):
            group = by_channel[channel_yt_id]
            channel = (
                db.query(Channel)
                .filter(Channel.youtube_id == channel_yt_id)
                .one_or_none()
            )
            if channel is None:
                log.info(
                    "%s: no shared-pool channels row; no mirror to repair",
                    channel_yt_id,
                )
                no_channel_row += 1
                continue

            mirror_key = (channel.avatar_r2_key or "").strip()
            # Distinct candidate keys, best first (live subscribers ahead
            # of removed ones, per the sort above).
            candidates: List[str] = []
            # Keys still owned by at least one live subscriber. A key held
            # only by removed rows is on borrowed time: purge_removed
            # (scripts/purge_removed.py, and the "Delete permanently"
            # route) deletes UserChannel.avatar_r2_key objects outright,
            # which would re-break the shared mirror for everyone else.
            live_keys: Set[str] = set()
            for row in group:
                k = (row.avatar_r2_key or "").strip()
                if not k:
                    continue
                if k not in candidates:
                    candidates.append(k)
                if row.removed_at is None:
                    live_keys.add(k)

            if mirror_key and mirror_key in candidates:
                log.info("%s: mirror already agrees (%s)", channel_yt_id, mirror_key)
                in_sync += 1
                continue

            # The two models disagree. Find out which pointer actually
            # has bytes behind it before touching anything.
            mirror_found: Optional[bool] = (
                _exists(mirror_key, exists_cache) if mirror_key else False
            )
            if mirror_found is None:
                log.warning(
                    "%s: could not check the current mirror (%s); changing nothing",
                    channel_yt_id,
                    mirror_key,
                )
                skipped_error += 1
                continue
            if mirror_found:
                log.info(
                    "%s: mirror differs from the user key but its object EXISTS "
                    "(%s); leaving the working pointer alone",
                    channel_yt_id,
                    mirror_key,
                )
                mirror_kept += 1
                continue

            replacement: Optional[str] = None
            errored = False
            for k in candidates:
                found = _exists(k, exists_cache)
                if found is None:
                    errored = True
                    continue
                if found:
                    replacement = k
                    break

            if replacement is None and errored:
                log.warning(
                    "%s: storage lookup failed for every user key; changing nothing",
                    channel_yt_id,
                )
                skipped_error += 1
                continue

            if replacement is None:
                log.error(
                    "%s: AVATAR MISSING FROM STORAGE - neither the mirror (%s) nor "
                    "any of the %d user key(s) %s has bytes. This needs a refetch "
                    "from YouTube, not a pointer fix. Nothing changed.",
                    channel_yt_id,
                    mirror_key or "(empty)",
                    len(candidates),
                    candidates,
                )
                missing_everywhere += 1
                continue

            if replacement not in live_keys:
                log.warning(
                    "%s: the only avatar bytes we can find (%s) sit under a "
                    "subscriber who already removed this channel. Repairing "
                    "anyway, because a working avatar now beats an empty "
                    "circle, but purge_removed will delete that object when "
                    "the grace window closes and the mirror will break "
                    "again. Re-run this script after the purge, or refetch "
                    "the avatar from YouTube.",
                    channel_yt_id,
                    replacement,
                )
                borrowed += 1

            log.info(
                "%s: %s mirror | old=%s (MISSING) -> new=%s (FOUND)",
                channel_yt_id,
                "repairing" if write else "would repair",
                mirror_key or "(empty)",
                replacement,
            )
            if write:
                channel.avatar_r2_key = replacement
            repaired += 1

        log.info(
            "summary: %d user_channels rows inspected across %d channels | "
            "%d %s (%d of them onto a removed subscriber's key, which purge "
            "will eventually delete) | %d already in sync | "
            "%d working mirrors left alone | "
            "%d avatars missing from storage entirely | %d without a channels row | "
            "%d skipped on storage errors",
            inspected,
            len(by_channel),
            repaired,
            "repaired" if write else "would be repaired",
            borrowed,
            in_sync,
            mirror_kept,
            missing_everywhere,
            no_channel_row,
            skipped_error,
        )

        # One commit for the whole run, so a mid-run failure leaves the
        # mirrors exactly as they were.
        if not write:
            log.info("dry run: rolling back")
            db.rollback()
        elif repaired:
            db.commit()
            log.info("committed %d mirror repair(s).", repaired)
        else:
            db.rollback()
            log.info("nothing to change.")
        return 0
    except Exception:
        log.exception("avatar mirror repair failed")
        db.rollback()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
