"""Deleting a channel eventually deletes its catalogue.

The owner deleted a channel, waited, re-added it, and every private
video title came straight back. Cause: the purge removed R2 objects and
the LEGACY per-user rows (UserChannel, UserChannelVideo) and stopped
there. The shared-pool Channel and Video rows - which is where the
titles actually live - were never deleted by anything, ever.

The pool is shared, so a single user's delete must not wipe rows other
people still use. But when the LAST subscriber leaves and the grace
window passes, nobody is using them and they are just a private
catalogue sitting on our disk forever.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app import archive
from app.models import (
    Channel,
    ChannelOwnership,
    User,
    UserChannelSubscription,
    Video,
)
from scripts.purge_removed import purge_orphaned_pool_channels

OLD = datetime.now(timezone.utc) - timedelta(days=40)
RECENT = datetime.now(timezone.utc) - timedelta(days=3)


def _user(db, uid):
    u = User(id=uid, username=uid, email=f"{uid}@x.com", password_hash="p")
    db.add(u)
    db.flush()
    return u


def _channel_with_video(db, yt, title="a video"):
    ch = archive.ensure_channel(db, yt)
    db.add(Video(
        channel_id=ch.id, youtube_id=f"v-{yt}", title=title,
        published_at=datetime.now(timezone.utc),
        privacy_at_discovery="private", privacy_current="private",
        visibility="sealed",
    ))
    db.flush()
    return ch


def _sub(db, user, ch, unsubscribed_at):
    db.add(UserChannelSubscription(
        user_id=user.id, channel_id=ch.id, unsubscribed_at=unsubscribed_at
    ))
    db.flush()


def test_long_abandoned_channel_and_its_videos_are_deleted(db):
    u = _user(db, "u1")
    ch = _channel_with_video(db, "UCgone", "archived clip")
    _sub(db, u, ch, unsubscribed_at=OLD)

    out = purge_orphaned_pool_channels(db)

    assert out == {"channels": 1, "videos": 1}
    assert db.query(Channel).filter(Channel.youtube_id == "UCgone").count() == 0
    assert db.query(Video).count() == 0


def test_channel_someone_still_tracks_is_untouched(db):
    alice, bob = _user(db, "alice"), _user(db, "bob")
    ch = _channel_with_video(db, "UCshared")
    _sub(db, alice, ch, unsubscribed_at=OLD)
    _sub(db, bob, ch, unsubscribed_at=None)  # bob still tracks it

    out = purge_orphaned_pool_channels(db)

    assert out == {"channels": 0, "videos": 0}
    assert db.query(Video).count() == 1, "bob's catalogue must survive"


def test_recently_removed_channel_is_kept_for_restore(db):
    """The 30-day grace is the whole point of 'Recently removed'."""
    u = _user(db, "u3")
    ch = _channel_with_video(db, "UCrecent")
    _sub(db, u, ch, unsubscribed_at=RECENT)

    assert purge_orphaned_pool_channels(db) == {"channels": 0, "videos": 0}
    assert db.query(Video).count() == 1


def test_brand_new_channel_mid_add_is_not_swept(db):
    """ensure_channel() runs before ensure_subscription(). A channel with
    no subscriptions yet must not be deleted out from under the add."""
    _channel_with_video(db, "UCbrandnew")

    assert purge_orphaned_pool_channels(db) == {"channels": 0, "videos": 0}
    assert db.query(Channel).count() == 1


def test_ownership_rows_go_too(db):
    u = _user(db, "u4")
    ch = _channel_with_video(db, "UCowned")
    _sub(db, u, ch, unsubscribed_at=OLD)
    db.add(ChannelOwnership(
        user_id=u.id, channel_id=ch.id, google_user_id="worker"
    ))
    db.flush()

    purge_orphaned_pool_channels(db)

    assert db.query(ChannelOwnership).count() == 0, (
        "a stale ownership row pointing at a deleted channel is dead weight"
    )


def test_dry_run_reports_without_deleting(db):
    u = _user(db, "u5")
    ch = _channel_with_video(db, "UCdry")
    _sub(db, u, ch, unsubscribed_at=OLD)

    out = purge_orphaned_pool_channels(db, dry_run=True)

    assert out == {"channels": 1, "videos": 1}
    assert db.query(Channel).count() == 1, "dry run must not delete"
    assert db.query(Video).count() == 1
