"""Smoke tests for app.archive (write-side helpers)."""
from __future__ import annotations

from datetime import datetime, timezone

from app import archive
from app.models import (
    Channel,
    ChannelOwnership,
    UserChannelSubscription,
    User,
    Video,
)


def _make_user(db, *, username="alice"):
    u = User(
        id=f"u-{username}",
        username=username,
        email=f"{username}@example.com",
        password_hash="$2b$12$placeholder",
    )
    db.add(u)
    db.flush()
    return u


def test_ensure_channel_creates_then_returns_existing(db):
    ch1 = archive.ensure_channel(db, "UCabc", title="Test")
    ch2 = archive.ensure_channel(db, "UCabc")
    assert ch1.id == ch2.id
    assert db.query(Channel).count() == 1


def test_ensure_subscription_reactivates_soft_deleted(db):
    user = _make_user(db)
    ch = archive.ensure_channel(db, "UCabc")

    sub = archive.ensure_subscription(db, user.id, ch.id)
    sub.unsubscribed_at = datetime.now(timezone.utc)
    db.flush()
    # Re-ensuring should clear the unsubscribed_at.
    sub_again = archive.ensure_subscription(db, user.id, ch.id)
    assert sub.id == sub_again.id
    assert sub_again.unsubscribed_at is None


def test_record_synced_video_cascades(db):
    user = _make_user(db)

    video = archive.record_synced_video(
        db,
        user_id=user.id,
        youtube_channel_id="UCxyz",
        youtube_video_id="vidA",
        channel_title="Channel XYZ",
        title="My Video",
        privacy="public",
        r2_key="videos/vidA/video.mp4",
        bytes_stored=1_000_000,
        google_user_id="google-1",
    )

    # Channel created
    ch = (
        db.query(Channel)
        .filter(Channel.youtube_id == "UCxyz")
        .one()
    )
    assert ch.title == "Channel XYZ"

    # Subscription created
    sub = (
        db.query(UserChannelSubscription)
        .filter(
            UserChannelSubscription.user_id == user.id,
            UserChannelSubscription.channel_id == ch.id,
        )
        .one()
    )
    assert sub.unsubscribed_at is None

    # Ownership created since we passed google_user_id
    own = (
        db.query(ChannelOwnership)
        .filter(
            ChannelOwnership.user_id == user.id,
            ChannelOwnership.channel_id == ch.id,
        )
        .one()
    )
    assert own.google_user_id == "google-1"

    # Video has the right values
    assert video.bytes_stored == 1_000_000
    assert video.r2_key == "videos/vidA/video.mp4"
    assert video.privacy_current == "public"
    assert video.synced_at is not None


def test_record_synced_video_updates_existing(db):
    user = _make_user(db)

    # First call creates everything
    archive.record_synced_video(
        db,
        user_id=user.id,
        youtube_channel_id="UCxyz",
        youtube_video_id="vidA",
        privacy="public",
    )

    # Second call upgrades the existing Video with sync data
    video = archive.record_synced_video(
        db,
        user_id=user.id,
        youtube_channel_id="UCxyz",
        youtube_video_id="vidA",
        privacy="public",
        r2_key="videos/vidA/video.mp4",
        bytes_stored=2_000_000,
    )

    assert db.query(Video).count() == 1
    assert video.bytes_stored == 2_000_000
    assert video.r2_key == "videos/vidA/video.mp4"


def test_record_synced_video_no_google_skips_ownership(db):
    user = _make_user(db)

    archive.record_synced_video(
        db,
        user_id=user.id,
        youtube_channel_id="UCxyz",
        youtube_video_id="vidA",
        privacy="public",
        google_user_id=None,
    )

    assert db.query(ChannelOwnership).count() == 0
    assert db.query(UserChannelSubscription).count() == 1
