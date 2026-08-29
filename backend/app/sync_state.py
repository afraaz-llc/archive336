"""What sync state is a video actually in, for one user.

This exists because "failed" had two definitions that disagreed.

The home page banner counts videos with a failed SyncJob that are not
queued and not stored - a job-derived set. The video listings reported
whatever ``status`` sat in the user's UserChannelVideo blob. Those two
answers differ whenever a video fails before it ever gets a row, which
is exactly what happens when the very first attempt fails: yt-dlp
reports the video is private, the job goes to ``failed``, and no
per-user row is ever written.

The visible symptom was a banner saying "3 videos failed to back up"
next to a list that could only ever show 2. Clicking through to a
number that does not match is worse than not linking at all, so the
count and the list now read from here.
"""
from __future__ import annotations

import json
from typing import Set

from sqlalchemy.orm import Session

from app.models import SyncJob, UserChannelVideo


def failed_video_ids(db: Session, user_id: str) -> Set[str]:
    """Videos this user has a real, outstanding failure on.

    A video counts as failed when it has at least one failed video job
    AND is not currently queued for another attempt AND we do not
    already hold the file. The last two are what keep the number
    honest: a video that failed once and then succeeded, or that is
    mid-retry, is not something the user needs to look at.

    Deliberately has no time window. A video that failed a month ago
    and was never retried is still not backed up, and quietly dropping
    it out of the count would mean the banner reads "everything is up
    to date" while a video is missing.
    """
    failed = {
        v
        for (v,) in db.query(SyncJob.video_id)
        .filter(
            SyncJob.user_id == user_id,
            SyncJob.kind == "video",
            SyncJob.status == "failed",
        )
        .distinct()
    }
    if not failed:
        return set()

    queued = {
        v
        for (v,) in db.query(SyncJob.video_id)
        .filter(
            SyncJob.user_id == user_id,
            SyncJob.kind == "video",
            SyncJob.status.in_(("pending", "running")),
            SyncJob.video_id.in_(failed),
        )
        .distinct()
    }

    stored = set()
    for row in db.query(UserChannelVideo).filter(
        UserChannelVideo.user_id == user_id,
        UserChannelVideo.video_id.in_(failed),
    ):
        try:
            if (json.loads(row.data_json) or {}).get("status") == "archived":
                stored.add(row.video_id)
        except (json.JSONDecodeError, TypeError):
            # An unparseable blob is not evidence that we hold the file,
            # so the video stays in the failed set rather than being
            # silently forgiven.
            continue

    return failed - queued - stored
