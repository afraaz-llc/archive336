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
from typing import Optional, Set

from sqlalchemy.orm import Session

from app.models import SyncJob, UserChannelVideo


# Errors that mean "there is nothing to download yet", not "we tried and
# could not". A scheduled livestream or an unstarted premiere has no
# file in existence, so counting it as a backup failure tells the user
# something is wrong when nothing is. It stops being one of these the
# moment it airs, at which point a real attempt can succeed or fail on
# its own terms.
#
# A video YouTube is still processing is the same fact at the other end:
# the upload landed but the playable file does not exist yet. The owner
# uploaded one at 20:37 and its card read a red "Failed" for the rest of
# the night, when all that had happened was that YouTube had not finished
# transcoding it.
_NOT_READY_MARKERS = (
    "live event will begin",
    "premieres in",
    "premiere will begin",
    "this live event will begin in",
    "processing this video",
    "still being processed",
)


def _is_not_ready(error: Optional[str]) -> bool:
    if not error:
        return False
    lowered = error.lower()
    return any(marker in lowered for marker in _NOT_READY_MARKERS)


def failed_video_ids(db: Session, user_id: str) -> Set[str]:
    """Videos this user has a real, outstanding failure on.

    A video counts as failed when it has at least one failed video job
    AND is not currently queued for another attempt AND we do not
    already hold the file AND YouTube has not confirmed it is gone. The last two are what keep the number
    honest: a video that failed once and then succeeded, or that is
    mid-retry, is not something the user needs to look at.

    Deliberately has no time window. A video that failed a month ago
    and was never retried is still not backed up, and quietly dropping
    it out of the count would mean the banner reads "everything is up
    to date" while a video is missing.
    """
    # Group by video rather than by job: what matters is whether the
    # video's most recent attempt was a real failure. A video whose only
    # failures are "not aired yet" is not something to alarm about.
    latest_error: dict = {}
    for vid, err, created in (
        db.query(SyncJob.video_id, SyncJob.error, SyncJob.created_at)
        .filter(
            SyncJob.user_id == user_id,
            SyncJob.kind == "video",
            SyncJob.status == "failed",
        )
        .order_by(SyncJob.created_at.asc())
    ):
        latest_error[vid] = err

    failed = {
        vid
        for vid, err in latest_error.items()
        if not _is_not_ready(err)
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
    gone = set()
    for row in db.query(UserChannelVideo).filter(
        UserChannelVideo.user_id == user_id,
        UserChannelVideo.video_id.in_(failed),
    ):
        try:
            status = (json.loads(row.data_json) or {}).get("status")
        except (json.JSONDecodeError, TypeError):
            # An unparseable blob is not evidence that we hold the file,
            # so the video stays in the failed set rather than being
            # silently forgiven.
            continue
        if status == "archived":
            stored.add(row.video_id)
        elif status == "deleted_on_youtube":
            # YouTube confirmed the video no longer exists, so there is
            # nothing left to back up and no attempt can succeed. Counting it
            # kept a deleted upload in the banner, retried daily, for good.
            gone.add(row.video_id)

    return failed - queued - stored - gone
