"""Bounds on how much work one channel-add can create.

Adding a channel now queues its whole back catalogue, so "however many the
catalogue happens to have" stopped being an acceptable answer. A 20,000
video channel is one paste away.

Two rules, both counted off the sync_jobs table itself so there is no new
column and no migration:
  - a per-user ceiling on outstanding work, across all channels
  - a give-up count, so a permanently-failing video is not re-queued
    forever now that the back-catalogue date rule is gone (that rule was
    incidentally throttling the retry loop)
"""
from __future__ import annotations

from app import auto_download
from app.models import SyncJob, User


def _user(db, uid="u1"):
    u = User(
        id=uid, username=uid, email=f"{uid}@x.com",
        password_hash="p", payment_status="active",
    )
    db.add(u)
    db.flush()
    return u


def _fill(db, user, n, *, status="pending", channel="UCx"):
    for i in range(n):
        db.add(SyncJob(
            user_id=user.id, channel_id=channel,
            video_id=f"filler-{channel}-{i}", kind="video", status=status,
        ))
    db.flush()


def test_enqueue_stops_at_the_cap(db):
    u = _user(db)
    _fill(db, u, auto_download.VIDEO_JOBS_MAX_OUTSTANDING - 3)

    created = auto_download.enqueue_downloads(
        db, user_id=u.id, channel_youtube_id="UCx",
        video_ids=[f"new-{i}" for i in range(50)],
    )

    assert created == 3, "only the room left under the cap is used"


def test_cap_counts_across_channels(db):
    """Six channels must not queue six caps' worth between them."""
    u = _user(db)
    _fill(db, u, auto_download.VIDEO_JOBS_MAX_OUTSTANDING, channel="UCother")

    created = auto_download.enqueue_downloads(
        db, user_id=u.id, channel_youtube_id="UCx", video_ids=["a", "b"],
    )

    assert created == 0


def test_cap_is_per_user(db):
    """One user filling their queue must not stop anyone else."""
    a, b = _user(db, "alice"), _user(db, "bob")
    _fill(db, a, auto_download.VIDEO_JOBS_MAX_OUTSTANDING)

    created = auto_download.enqueue_downloads(
        db, user_id=b.id, channel_youtube_id="UCx", video_ids=["a", "b"],
    )

    assert created == 2


def test_repeatedly_failing_video_is_given_up_on(db):
    u = _user(db)
    for _ in range(auto_download.MAX_AUTO_ATTEMPTS):
        db.add(SyncJob(
            user_id=u.id, channel_id="UCx", video_id="cursed",
            kind="video", status="failed", error="unavailable",
        ))
    db.flush()

    created = auto_download.enqueue_downloads(
        db, user_id=u.id, channel_youtube_id="UCx",
        video_ids=["cursed", "fine"],
    )

    assert created == 1, "the cursed video is dropped, the healthy one is not"


def test_cancelled_work_does_not_count_against_a_video(db):
    """Removing a channel marks its pending jobs failed. Re-adding it must
    not find every video pre-condemned by work the user called off."""
    u = _user(db)
    for _ in range(auto_download.MAX_AUTO_ATTEMPTS + 2):
        db.add(SyncJob(
            user_id=u.id, channel_id="UCx", video_id="v1",
            kind="video", status="failed", error="cancelled: channel removed",
        ))
    db.flush()

    created = auto_download.enqueue_downloads(
        db, user_id=u.id, channel_youtube_id="UCx", video_ids=["v1"],
    )

    assert created == 1


def test_a_full_storage_bucket_does_not_condemn_a_video(db):
    """The regression, taken from production.

    The bucket had no lifecycle rule, so dead object versions accumulated
    until Backblaze refused every upload with "storage cap exceeded". The
    worker kept downloading fine; only the PUT failed. Five rounds of that
    and eight of the owner's videos were written off permanently - all of
    them downloadable, none ever retried, and the queue sat empty while
    the page said "Not synced".

    A failure that describes our storage is not evidence about his video.
    """
    u = _user(db)
    for _ in range(auto_download.MAX_AUTO_ATTEMPTS + 3):
        db.add(SyncJob(
            user_id=u.id, channel_id="UCx", video_id="v1",
            kind="video", status="failed",
            error=(
                "r2 put http 403 Forbidden: <Error><Code>AccessDenied</Code>"
                "<Message>Cannot upload files, storage cap exceeded.</Message>"
                "</Error>"
            ),
        ))
    db.flush()

    created = auto_download.enqueue_downloads(
        db, user_id=u.id, channel_youtube_id="UCx", video_ids=["v1"],
    )

    assert created == 1, "our bucket being full is not the video's fault"


def test_yt_dlp_breaking_does_not_condemn_a_video(db):
    """The other half of the same outage: yt-dlp's extractor broke against
    a YouTube change. Upstream weather, not a property of the file."""
    u = _user(db)
    for _ in range(auto_download.MAX_AUTO_ATTEMPTS + 1):
        db.add(SyncJob(
            user_id=u.id, channel_id="UCx", video_id="v1",
            kind="video", status="failed",
            error=(
                "yt-dlp failed: WARNING: [youtube] unable to extract yt "
                "initial data; please report this issue"
            ),
        ))
    db.flush()

    created = auto_download.enqueue_downloads(
        db, user_id=u.id, channel_youtube_id="UCx", video_ids=["v1"],
    )

    assert created == 1


def test_an_age_gate_still_counts(db):
    """The counter must not become toothless. A video we genuinely cannot
    fetch with the credentials we hold is a real answer, and retrying it
    every 30 minutes forever is its own bug."""
    u = _user(db)
    for _ in range(auto_download.MAX_AUTO_ATTEMPTS):
        db.add(SyncJob(
            user_id=u.id, channel_id="UCx", video_id="gated",
            kind="video", status="failed",
            error=(
                "yt-dlp failed: ERROR: [youtube] s9mA5eNJoBY: Sign in to "
                "confirm your age. This video may be inappropriate for some "
                "users."
            ),
        ))
    db.flush()

    created = auto_download.enqueue_downloads(
        db, user_id=u.id, channel_youtube_id="UCx",
        video_ids=["gated", "fine"],
    )

    assert created == 1, "the age-gated one is dropped, the healthy one is not"


def test_mixed_failures_only_count_the_real_ones(db):
    """Four environmental failures plus one age gate is one strike, not
    five. This is the shape the production rows actually had."""
    u = _user(db)
    errors = [
        "r2 put http 403 Forbidden: storage cap exceeded.",
        "yt-dlp failed: WARNING: [youtube] unable to extract yt initial data",
        "r2 put http 503 Service Unavailable",
        "DownloadError('ERROR: unable to download video data: HTTP Error 403')",
        "yt-dlp failed: ERROR: [youtube] x: Sign in to confirm your age.",
    ]
    for e in errors:
        db.add(SyncJob(
            user_id=u.id, channel_id="UCx", video_id="v1",
            kind="video", status="failed", error=e,
        ))
    db.flush()

    created = auto_download.enqueue_downloads(
        db, user_id=u.id, channel_youtube_id="UCx", video_ids=["v1"],
    )

    assert created == 1


def test_duplicate_active_job_is_impossible(db):
    """The database enforces it, not the callers.

    Four call sites create video jobs and each had its own read-then-write
    dedupe. The sweep runs in a different process from the API, so its
    snapshot can go stale between the read and the commit - and that window
    widens with queue depth. A duplicate is not cosmetic: it downloads the
    same video twice and bills the storage twice.
    """
    from sqlalchemy.exc import IntegrityError

    u = _user(db)
    db.add(SyncJob(
        user_id=u.id, channel_id="UCx", video_id="v1",
        kind="video", status="pending",
    ))
    db.flush()

    db.add(SyncJob(
        user_id=u.id, channel_id="UCx", video_id="v1",
        kind="video", status="pending",
    ))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
    else:
        raise AssertionError("a second ACTIVE job for the same video got in")


def test_a_finished_job_does_not_block_a_new_one(db):
    """The index is partial on purpose. Terminal rows accumulate - the
    give-up counter reads them - and must never stop a re-queue."""
    u = _user(db)
    db.add(SyncJob(
        user_id=u.id, channel_id="UCx", video_id="v1",
        kind="video", status="done",
    ))
    db.flush()

    created = auto_download.enqueue_downloads(
        db, user_id=u.id, channel_youtube_id="UCx", video_ids=["v1"],
    )
    db.flush()

    assert created == 1


def test_captions_job_does_not_block_the_video_job(db):
    """kind is part of the key: a pending captions job must not mask the
    video job for the same video."""
    u = _user(db)
    db.add(SyncJob(
        user_id=u.id, channel_id="UCx", video_id="v1",
        kind="captions", status="pending",
    ))
    db.flush()

    created = auto_download.enqueue_downloads(
        db, user_id=u.id, channel_youtube_id="UCx", video_ids=["v1"],
    )
    db.flush()

    assert created == 1


def test_authenticating_forgives_permission_failures(db):
    """Authenticating a channel must help the videos it exists to unlock.

    Two of the owner's videos sat at exactly MAX_AUTO_ATTEMPTS on "Sign in
    to confirm your age" - five honest refusals from before there were any
    credentials to refuse with. He then authenticated the channel, the one
    action that fixes them, and they stayed written off.
    """
    u = _user(db)
    for _ in range(auto_download.MAX_AUTO_ATTEMPTS):
        db.add(SyncJob(
            user_id=u.id, channel_id="UCx", video_id="gated",
            kind="video", status="failed",
            error="yt-dlp failed: ERROR: [youtube] x: Sign in to confirm your age.",
        ))
    db.flush()
    assert auto_download.enqueue_downloads(
        db, user_id=u.id, channel_youtube_id="UCx", video_ids=["gated"],
    ) == 0, "given up on before authenticating"

    auto_download.forgive_permission_failures(
        db, user_id=u.id, channel_youtube_id="UCx",
    )
    db.flush()

    assert auto_download.enqueue_downloads(
        db, user_id=u.id, channel_youtube_id="UCx", video_ids=["gated"],
    ) == 1, "authenticating gives it a fresh start"


def test_forgiveness_does_not_touch_a_deleted_video(db):
    """Only failures authentication could plausibly fix are forgiven. A
    removed video is still removed no matter who is signed in."""
    u = _user(db)
    for _ in range(auto_download.MAX_AUTO_ATTEMPTS):
        db.add(SyncJob(
            user_id=u.id, channel_id="UCx", video_id="gone",
            kind="video", status="failed",
            error="yt-dlp failed: ERROR: [youtube] x: Video unavailable",
        ))
    db.flush()

    forgiven = auto_download.forgive_permission_failures(
        db, user_id=u.id, channel_youtube_id="UCx",
    )
    db.flush()

    assert forgiven == 0
    assert auto_download.enqueue_downloads(
        db, user_id=u.id, channel_youtube_id="UCx", video_ids=["gone"],
    ) == 0
