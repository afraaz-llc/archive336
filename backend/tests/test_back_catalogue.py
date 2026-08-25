"""Adding a channel backs up the channel, not just its future uploads.

The owner, testing as their own customer, found only 1 of 3 videos saved -
and the one that saved was an accident (an old failed job that a retry
sweep happened to resurrect). Nothing was backing up the catalogue at all.

pending_new_uploads skipped anything published BEFORE the channel was
added: "back catalogue - explicit Sync only". So adding a channel to a
backup service backed up none of the videos already on it, and nothing on
any screen said so. For a product whose promise is "your channel is backed
up", that was the single biggest gap in it.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app import auto_download, archive
from app.models import (
    ChannelOwnership,
    User,
    UserChannel,
    UserChannelSubscription,
    UserChannelVideo,
    Video,
)

LONG_AGO = datetime.now(timezone.utc) - timedelta(days=900)


def _user(db, uid="u1"):
    u = User(
        id=uid, username=uid, email=f"{uid}@x.com",
        password_hash="p", payment_status="active",
    )
    db.add(u)
    db.flush()
    return u


def _track(db, user, yt="UCme"):
    ch = archive.ensure_channel(db, yt)
    db.add(UserChannelSubscription(user_id=user.id, channel_id=ch.id))
    uc = UserChannel(
        user_id=user.id, channel_id=yt, google_user_id=None,
        data_json=json.dumps({
            "id": yt,
            # Added just now: every video below predates it.
            "addedAt": datetime.now(timezone.utc).isoformat(),
            "settings": {"active": True, "downloadNewVideos": True},
        }),
    )
    db.add(uc)
    db.flush()
    return ch, uc


def _video(db, ch, vid, *, visibility="open", privacy="public", r2_key=None):
    v = Video(
        channel_id=ch.id, youtube_id=vid, title=vid, published_at=LONG_AGO,
        privacy_at_discovery=privacy, privacy_current=privacy,
        visibility=visibility, r2_key=r2_key,
    )
    db.add(v)
    db.flush()
    return v


def test_old_public_catalogue_is_queued(db):
    """The headline. A channel added today whose videos are years old."""
    u = _user(db)
    ch, uc = _track(db, u)
    _video(db, ch, "old-1")
    _video(db, ch, "old-2")

    assert sorted(auto_download.pending_new_uploads(db, uc)) == ["old-1", "old-2"]


def test_sealed_videos_still_need_authentication(db):
    """Backing up everything does not mean seeing everything."""
    u = _user(db)
    ch, uc = _track(db, u)
    _video(db, ch, "public-one")
    _video(db, ch, "owners-private", visibility="sealed", privacy="private")

    assert auto_download.pending_new_uploads(db, uc) == ["public-one"]


def test_owner_gets_their_sealed_videos_too(db):
    u = _user(db)
    ch, uc = _track(db, u)
    db.add(ChannelOwnership(
        user_id=u.id, channel_id=ch.id, google_user_id="worker"
    ))
    _video(db, ch, "owners-private", visibility="sealed", privacy="private")
    db.flush()

    assert auto_download.pending_new_uploads(db, uc) == ["owners-private"]


def test_another_subscribers_archive_does_not_strand_yours(db):
    """Regression for the shared-pool r2_key trap.

    Video.r2_key names whichever single subscriber archived the file, not
    the caller. Filtering the queue on it meant that once ANY user archived
    a video, it stopped being queued for every OTHER subscriber - so the
    second person to track a popular channel silently got nothing.
    """
    alice, bob = _user(db, "alice"), _user(db, "bob")
    ch, _ = _track(db, alice)
    _, bobs_uc = _track(db, bob)
    # Alice already archived it: the shared row carries HER key.
    _video(db, ch, "popular", r2_key="users/alice/videos/popular/video.mp4")

    assert auto_download.pending_new_uploads(db, bobs_uc) == ["popular"], (
        "another user's copy must not stand in for this user's backup"
    )


def test_already_archived_by_this_user_is_not_requeued(db):
    u = _user(db)
    ch, uc = _track(db, u)
    _video(db, ch, "done-already")
    db.add(UserChannelVideo(
        user_id=u.id, channel_id="UCme", video_id="done-already",
        data_json=json.dumps({"status": "archived"}),
    ))
    db.flush()

    assert auto_download.pending_new_uploads(db, uc) == []
