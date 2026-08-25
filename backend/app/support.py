"""Support conversations, and the account state that makes them answerable.

Every question a user asks about a backup tool is a question about
state they cannot see. "Why isn't it syncing" has at least six answers -
paused channel, offline worker, lapsed card, full bucket, given-up
video, unauthenticated channel - and the user can distinguish none of
them. Asking them to is asking a non-technical person to debug the
product on the maintainer's behalf.

So the snapshot is taken here, server-side, at send time. It is the
difference between a three-message round trip and a reply written
immediately, and it is the whole reason this is worth building rather
than dropping in a chat widget: no third party can attach it.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (
    Channel,
    SupportMessage,
    SyncJob,
    User,
    UserChannelSubscription,
    WorkerYoutubeConnection,
)

SUPPORT_KINDS = ("bug", "feature", "question")
MAX_BODY_CHARS = 5000


def account_snapshot(db: Session, user: User) -> Dict[str, Any]:
    """What the maintainer would otherwise have to go and look up.

    Deliberately facts, not diagnosis: counts, states and the most
    recent errors. Reading the situation is the maintainer's job and a
    stored guess would age badly - the snapshot is evidence, and evidence
    should not have opinions in it.
    """
    now = datetime.now(timezone.utc)

    subs = (
        db.query(UserChannelSubscription, Channel)
        .join(Channel, Channel.id == UserChannelSubscription.channel_id)
        .filter(
            UserChannelSubscription.user_id == user.id,
            UserChannelSubscription.unsubscribed_at.is_(None),
        )
        .all()
    )
    channels = []
    for sub, ch in subs:
        try:
            settings = json.loads(sub.settings_json) if sub.settings_json else {}
        except (TypeError, ValueError):
            settings = {}
        channels.append(
            {
                "title": ch.title or ch.youtube_id,
                "youtubeId": ch.youtube_id,
                "active": bool(settings.get("active", True)),
            }
        )

    counts: Dict[str, int] = {}
    for status, n in (
        db.query(SyncJob.status, func.count(SyncJob.id))
        .filter(SyncJob.user_id == user.id, SyncJob.kind == "video")
        .group_by(SyncJob.status)
    ):
        counts[status] = n

    # Distinct errors, not the five most recent.
    #
    # One video failing repeatedly fills the whole list with the same
    # sentence and hides every other failure - the first real alert this
    # produced showed "This video is private" four times and nothing
    # else. What the maintainer needs is the RANGE of what is going
    # wrong; how often is a question the admin panel answers.
    seen: Dict[str, int] = {}
    for (e,) in (
        db.query(SyncJob.error)
        .filter(
            SyncJob.user_id == user.id,
            SyncJob.status == "failed",
            SyncJob.error.isnot(None),
            SyncJob.created_at >= now - timedelta(days=2),
        )
        .order_by(SyncJob.created_at.desc())
        .limit(200)
    ):
        if not e:
            continue
        key = e[:160]
        seen[key] = seen.get(key, 0) + 1
        if len(seen) >= 5:
            break
    recent_errors = [
        f"{err}  (x{n})" if n > 1 else err for err, n in seen.items()
    ]

    conn = db.get(WorkerYoutubeConnection, user.id)
    reported = conn.reported_at if conn else None
    if reported is not None and reported.tzinfo is None:
        reported = reported.replace(tzinfo=timezone.utc)

    return {
        "capturedAt": now.isoformat(),
        "user": {
            "username": user.username,
            "email": user.email,
            "tier": getattr(user, "tier", None),
            "paymentStatus": getattr(user, "payment_status", None),
        },
        "channels": channels,
        "jobs": counts,
        "worker": {
            "connected": bool(conn and conn.connected),
            "cookieCount": conn.cookie_count if conn else 0,
            "reportedAt": reported.isoformat() if reported else None,
        },
        "recentErrors": recent_errors,
    }


def snapshot_lines(snapshot: Dict[str, Any]) -> str:
    """The snapshot as something readable in an email."""
    u = snapshot.get("user", {})
    w = snapshot.get("worker", {})
    jobs = snapshot.get("jobs", {})
    chans = snapshot.get("channels", [])
    active = sum(1 for c in chans if c.get("active"))
    lines = [
        f"{u.get('username')} <{u.get('email')}> - {u.get('tier')}, "
        f"payment {u.get('paymentStatus')}",
        f"channels: {len(chans)} ({active} active)",
        "jobs: " + (", ".join(f"{k} {v}" for k, v in sorted(jobs.items())) or "none"),
        "worker: "
        + (
            f"connected, {w.get('cookieCount')} cookies, last seen {w.get('reportedAt')}"
            if w.get("connected")
            else "not connected"
        ),
    ]
    errs = snapshot.get("recentErrors") or []
    if errs:
        lines.append("recent errors:")
        lines.extend(f"  - {e}" for e in errs)
    return "\n".join(lines)


def thread_for(db: Session, user_id: str) -> list[SupportMessage]:
    return (
        db.query(SupportMessage)
        .filter(SupportMessage.user_id == user_id)
        .order_by(SupportMessage.created_at.asc())
        .all()
    )
