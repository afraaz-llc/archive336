"""One-off: ask the worker what our long videos actually are.

The Type filter offers Livestream, and until now nothing was answering
it - the reading comes from yt-dlp's sidecar, which only the worker sees.
Downloads and comments passes report it from here on, but an archive that
already exists would be typed over the course of a month, because that is
how often comments refresh.

So this walks the videos that could plausibly be streams (long ones - a
forty-second clip is not a livestream) through a metadata job, which
downloads nothing and takes a couple of seconds each.

It tops the queue up in small batches and waits for the worker to drain
them, rather than dropping hundreds of jobs on it at once: the worker
runs on someone's laptop and shares this queue with real downloads.

    python scripts/backfill_video_type.py --user <id> [--min-seconds 600]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import SessionLocal  # noqa: E402
from app.models import SyncJob, UserChannelVideo  # noqa: E402

BATCH = 20
POLL_SECONDS = 20


def candidates(db, user_id: str, min_seconds: int):
    """Long videos with no livestream reading yet, newest ids last."""
    out = []
    for row in db.query(UserChannelVideo).filter(
        UserChannelVideo.user_id == user_id
    ):
        try:
            data = json.loads(row.data_json) or {}
        except (json.JSONDecodeError, TypeError):
            continue
        if "wasLive" in data:
            continue
        duration = data.get("durationSec")
        if not isinstance(duration, int) or duration < min_seconds:
            continue
        out.append((row.channel_id, row.video_id))
    return out


def outstanding(db, user_id: str) -> int:
    return (
        db.query(SyncJob)
        .filter(
            SyncJob.user_id == user_id,
            SyncJob.kind == "metadata",
            SyncJob.status.in_(["pending", "running"]),
        )
        .count()
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--user", required=True)
    ap.add_argument("--min-seconds", type=int, default=600)
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    db = SessionLocal()
    todo = candidates(db, args.user, args.min_seconds)
    print(f"{len(todo)} videos with no type reading and >= {args.min_seconds}s")
    if args.dry or not todo:
        return 0

    queued = 0
    while todo:
        room = BATCH - outstanding(db, args.user)
        if room <= 0:
            time.sleep(POLL_SECONDS)
            db.expire_all()
            continue
        for channel_id, video_id in todo[:room]:
            db.add(
                SyncJob(
                    user_id=args.user,
                    channel_id=channel_id,
                    video_id=video_id,
                    kind="metadata",
                    status="pending",
                )
            )
            queued += 1
        db.commit()
        todo = todo[room:]
        print(f"queued {queued}, {len(todo)} left", flush=True)
        time.sleep(POLL_SECONDS)
        db.expire_all()

    print(f"done: {queued} metadata jobs queued")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
