"""Is anyone's backup silently stopped?

The failure this exists for happened in production: the storage bucket
filled, every upload started 403ing, and the queue stalled completely.
Nothing anywhere said so. The app reported "running", the website showed
channels as active, and it was found only because the owner happened to
ask why a video had not appeared. With one customer that costs an
evening. With fifty it is fifty people whose backups stopped and who
will not find out until they need a file that was never captured.

The signal has to be "work exists and is not moving", not "an error
happened". Individual errors are normal - videos get deleted, YouTube
rate-limits, a laptop closes mid-download - and alerting on them would
train the operator to ignore the alerts. A queue that has not advanced
while a worker is alive to advance it is different: that is the shape of
every systemic outage, whatever caused it.

Deliberately NOT alerting on a worker that is simply offline. A closed
laptop is the customer's business and the most common state in the
product; paging the operator for it would bury the real signal. What
gets reported is a worker that IS connected and still not making
progress.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from app.models import SyncJob, User, WorkerYoutubeConnection


# How recently a worker must have checked in to count as present. The
# worker reports on a 60s heartbeat, so an hour is generous enough to
# survive a restart or a slow poll without calling a live worker dead.
WORKER_ALIVE_WINDOW = timedelta(hours=1)

# How long a queue may sit without a single completion before it counts
# as stalled. Long enough that one big video in flight is not an alarm -
# the 7h videos in the owner's own archive would otherwise trip it - and
# short enough that a real outage surfaces the same day.
STALL_WINDOW = timedelta(hours=8)

# How many failures sharing one message make it systemic rather than a
# run of unlucky videos.
FAILURE_STORM_COUNT = 10
FAILURE_STORM_WINDOW = timedelta(hours=1)


@dataclass
class StalledUser:
    user_id: str
    username: str
    pending: int
    last_done_at: Optional[datetime]


@dataclass
class FailureStorm:
    error: str
    count: int
    # Which queue produced it. Without this the alert cannot tell a
    # stalled backup from the nightly comment rescan meeting a stale
    # session, and it named the wrong one.
    kind: str = "video"


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite hands back naive datetimes for values we stored as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def find_stalled_users(db: Session, *, now: Optional[datetime] = None) -> List[StalledUser]:
    """Users with queued work, a live worker, and no recent completions."""
    when = now or datetime.now(timezone.utc)
    out: List[StalledUser] = []

    rows = (
        db.query(SyncJob.user_id, SyncJob.status, SyncJob.finished_at)
        .filter(SyncJob.kind == "video")
        .all()
    )
    by_user: dict[str, dict] = {}
    for user_id, status, finished_at in rows:
        acc = by_user.setdefault(user_id, {"pending": 0, "last_done": None})
        if status in ("pending", "running"):
            acc["pending"] += 1
        elif status == "done":
            ts = _aware(finished_at)
            if ts and (acc["last_done"] is None or ts > acc["last_done"]):
                acc["last_done"] = ts

    for user_id, acc in by_user.items():
        if acc["pending"] <= 0:
            continue
        last_done = acc["last_done"]
        if last_done is not None and when - last_done < STALL_WINDOW:
            continue  # something finished recently; the queue is moving

        conn = db.get(WorkerYoutubeConnection, user_id)
        reported = _aware(conn.reported_at) if conn else None
        if not conn or not conn.connected or reported is None:
            continue  # no worker to blame; not an operator problem
        if when - reported > WORKER_ALIVE_WINDOW:
            continue  # worker is gone, which is the customer's own doing

        user = db.get(User, user_id)
        out.append(
            StalledUser(
                user_id=user_id,
                username=user.username if user else user_id,
                pending=acc["pending"],
                last_done_at=last_done,
            )
        )
    return out


def find_failure_storms(
    db: Session, *, now: Optional[datetime] = None
) -> List[FailureStorm]:
    """Errors repeating often enough to be systemic rather than unlucky.

    Grouped on a prefix of the message because the interesting failures
    carry per-video detail - "yt-dlp failed: ERROR: [youtube] <id>: ..."
    is one problem, not five, and grouping on the whole string would
    hide exactly the storms worth seeing.
    """
    when = now or datetime.now(timezone.utc)
    since = when - FAILURE_STORM_WINDOW
    counts: dict[tuple[str, str], int] = {}
    for error, kind in (
        db.query(SyncJob.error, SyncJob.kind)
        .filter(
            SyncJob.status == "failed",
            SyncJob.error.isnot(None),
            SyncJob.created_at >= since,
        )
        .all()
    ):
        if not error or error.lower().startswith("cancelled:"):
            continue
        key = (error[:60], kind or "video")
        counts[key] = counts.get(key, 0) + 1
    return [
        FailureStorm(error=e, count=n, kind=k)
        for (e, k), n in sorted(counts.items(), key=lambda kv: -kv[1])
        if n >= FAILURE_STORM_COUNT
    ]
