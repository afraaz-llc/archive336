"""Sanity check for compute_user_byte_hours_v2 (shared-pool model).

Doesn't try to exhaustively cover billing math (the old function has
its own legacy tests); just enough to confirm the new join works, the
grace window is honored, and the sealed tier bills the owner for
exactly as long as we hold the bytes for them - no longer (revoking
authentication does not stop the meter) and no shorter (removing the
channel does).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.billing import compute_user_byte_hours_v2
from app.models import (
    Channel,
    ChannelOwnership,
    User,
    UserChannel,
    UserChannelSubscription,
    Video,
    visibility_for_privacy,
)

GB = 1_000_000_000


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


def _make_channel(db, *, youtube_id="UCabc"):
    ch = Channel(
        id=f"ch-{youtube_id}",
        youtube_id=youtube_id,
        title=youtube_id,
    )
    db.add(ch)
    db.flush()
    return ch


def _make_video(
    db,
    channel,
    *,
    privacy="public",
    bytes_stored=1_000_000_000,  # 1 GB
    synced_at,
    youtube_id="vidA",
):
    v = Video(
        id=f"v-{youtube_id}",
        channel_id=channel.id,
        youtube_id=youtube_id,
        title=youtube_id,
        published_at=synced_at,
        privacy_at_discovery=privacy,
        privacy_current=privacy,
        # The column defaults to "open", so a helper that only sets the
        # privacy pair produces an OPEN video no matter what privacy it
        # was handed - which silently routed every "private video" test
        # through the subscription path. Stamp it the same way
        # archive.ensure_video() does.
        visibility=visibility_for_privacy(privacy),
        bytes_stored=bytes_stored,
        r2_key=f"videos/{youtube_id}/video.mp4",
        synced_at=synced_at,
    )
    db.add(v)
    db.flush()
    return v


def test_public_video_byte_hours_simple(db):
    """1 GB stored for 24 hours through a subscription = 1 GB * 24 byte-hours."""
    user = _make_user(db)
    ch = _make_channel(db)
    now = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    sub = UserChannelSubscription(
        user_id=user.id,
        channel_id=ch.id,
        subscribed_at=now - timedelta(days=10),
    )
    db.add(sub)
    _make_video(
        db,
        ch,
        privacy="public",
        bytes_stored=1_000_000_000,
        synced_at=now - timedelta(days=10),
    )
    db.flush()

    start = now
    end = now + timedelta(hours=24)
    total = compute_user_byte_hours_v2(db, user.id, start, end)
    # Expect 1e9 bytes * 24 hours = 2.4e10 byte-hours
    assert abs(total - 2.4e10) < 1.0


def test_private_video_billed_only_to_owner(db):
    """A private video bills only the user with a ChannelOwnership.
    A subscriber without ownership pays $0 on it."""
    ch = _make_channel(db)
    now = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    _make_video(
        db,
        ch,
        privacy="private",
        bytes_stored=GB,
        synced_at=now - timedelta(days=10),
    )

    # Subscriber only - no ownership.
    alice = _make_user(db, username="alice")
    db.add(
        UserChannelSubscription(
            user_id=alice.id,
            channel_id=ch.id,
            subscribed_at=now - timedelta(days=10),
        )
    )

    # Owner. Also subscribed, because that is what actually happens:
    # archive.ensure_video() subscribes whoever synced the files, so
    # the owner whose worker pulled the sealed bytes always has a
    # subscription row. Ownership is what makes the sealed bytes HIS
    # rather than Alice's.
    bob = _make_user(db, username="bob")
    db.add(
        UserChannelSubscription(
            user_id=bob.id,
            channel_id=ch.id,
            subscribed_at=now - timedelta(days=10),
        )
    )
    db.add(
        ChannelOwnership(
            user_id=bob.id,
            channel_id=ch.id,
            google_user_id="g-bob",
            authenticated_at=now - timedelta(days=10),
        )
    )
    db.flush()

    start = now
    end = now + timedelta(hours=10)
    # Alice is subscribed but owns nothing, so the sealed bytes are not
    # hers at any price.
    assert compute_user_byte_hours_v2(db, alice.id, start, end) == 0.0
    assert abs(compute_user_byte_hours_v2(db, bob.id, start, end) - GB * 10) < 1.0


def test_billing_stops_at_unsubscribe_not_grace(db):
    """Billing stops the instant the user unsubscribes — the 30-day
    grace window is NOT billed (the company eats the R2 cost for
    that period as a UX investment, matching the remove-channel
    confirmation dialog's promise)."""
    user = _make_user(db)
    ch = _make_channel(db)
    now = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    sub = UserChannelSubscription(
        user_id=user.id,
        channel_id=ch.id,
        subscribed_at=now - timedelta(days=60),
        unsubscribed_at=now - timedelta(days=5),  # unsub 5 days ago
    )
    db.add(sub)
    _make_video(
        db,
        ch,
        privacy="public",
        bytes_stored=1_000_000_000,
        synced_at=now - timedelta(days=60),
    )
    db.flush()

    # Window is [-6 days, +10 days] = 16 days total. Unsubscribe was
    # at -5 days. User was actively subscribed from -6 to -5 = 1 day
    # of billable storage. The remaining 15 days post-unsubscribe
    # are grace and NOT billed.
    start = now - timedelta(days=6)
    end = now + timedelta(days=10)
    total = compute_user_byte_hours_v2(db, user.id, start, end)
    expected = 1_000_000_000 * 1 * 24  # 1 day × 24 hours
    assert abs(total - expected) < 1.0

    # Window starts AFTER unsubscribe entirely: zero billing.
    far_start = now + timedelta(days=1)
    far_end = now + timedelta(days=10)
    assert compute_user_byte_hours_v2(db, user.id, far_start, far_end) == 0.0


def test_pre_subscription_videos_dont_back_charge(db):
    """A video uploaded BEFORE the user subscribed shouldn't get
    billed for the pre-subscription period."""
    user = _make_user(db)
    ch = _make_channel(db)
    now = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    sub_at = now - timedelta(hours=10)
    db.add(
        UserChannelSubscription(
            user_id=user.id,
            channel_id=ch.id,
            subscribed_at=sub_at,
        )
    )
    # Video archived 24 hours ago, well before subscription
    _make_video(
        db,
        ch,
        privacy="public",
        bytes_stored=1_000_000_000,
        synced_at=now - timedelta(hours=24),
    )
    db.flush()

    start = now - timedelta(hours=24)
    end = now
    total = compute_user_byte_hours_v2(db, user.id, start, end)
    # User should be billed only for the 10 hours they were subscribed
    expected = 1_000_000_000 * 10
    assert abs(total - expected) < 1.0


def _owner_setup(db, now, *, username="owner", subscribed=True):
    """Owner of a channel holding one 1 GB sealed video synced 10 days
    ago. Returns (user, channel, ownership)."""
    user = _make_user(db, username=username)
    ch = _make_channel(db, youtube_id=f"UC-{username}")
    _make_video(
        db,
        ch,
        privacy="private",
        bytes_stored=GB,
        synced_at=now - timedelta(days=10),
        youtube_id=f"vid-{username}",
    )
    if subscribed:
        db.add(
            UserChannelSubscription(
                user_id=user.id,
                channel_id=ch.id,
                subscribed_at=now - timedelta(days=10),
            )
        )
    own = ChannelOwnership(
        user_id=user.id,
        channel_id=ch.id,
        google_user_id=f"g-{username}",
        authenticated_at=now - timedelta(days=10),
    )
    db.add(own)
    db.flush()
    return user, ch, own


def test_revoking_authentication_does_not_stop_sealed_meter(db):
    """Revoke is a permission change, not a billing change. We still
    hold every sealed file, so the meter runs for the whole window."""
    now = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    user, _ch, own = _owner_setup(db, now)

    # User hits Revoke 3 hours into the window. Both revoke columns go
    # up; nothing is deleted.
    own.revoked_at = now + timedelta(hours=3)
    own.user_revoked_at = now + timedelta(hours=3)
    db.flush()

    total = compute_user_byte_hours_v2(db, user.id, now, now + timedelta(hours=24))
    assert abs(total - GB * 24) < 1.0


def test_owner_without_subscription_meter_is_bounded(db):
    """The overcharge regression. An owner with no UserChannelSubscription
    row still has a stop: removing the channel sets the legacy
    UserChannel.removed_at, which is the same instant the storage ledger
    closes every StorageObject for that channel. Before the fix the
    missing subscription key read as None ("nothing ended it") and the
    meter ran to the end of the period forever.
    """
    now = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    user, ch, _own = _owner_setup(db, now, subscribed=False)
    db.add(
        UserChannel(
            user_id=user.id,
            channel_id=ch.youtube_id,
            data_json="{}",
            removed_at=now + timedelta(hours=6),
        )
    )
    db.flush()

    total = compute_user_byte_hours_v2(db, user.id, now, now + timedelta(hours=24))
    # 6 billable hours, not 24.
    assert abs(total - GB * 6) < 1.0

    # And nothing at all once the window starts after the removal.
    later = compute_user_byte_hours_v2(
        db, user.id, now + timedelta(hours=12), now + timedelta(hours=24)
    )
    assert later == 0.0


def test_ownership_alone_is_not_a_storage_relationship(db):
    """A user with ownership but no subscription AND no legacy tracking
    row never asked us to hold this channel - the worker records
    ownership for any channel the Google login owns, including ones the
    user does not track. Billing them would be an overcharge they have
    no way to stop, so it is exactly zero.
    """
    now = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    user, _ch, _own = _owner_setup(db, now, subscribed=False)

    total = compute_user_byte_hours_v2(db, user.id, now, now + timedelta(hours=24))
    assert total == 0.0


def test_removing_channel_stops_open_and_sealed_together(db):
    """Remove Channel is the one action that stops the bill, and it
    stops both tiers at the same instant."""
    now = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    user = _make_user(db, username="carol")
    ch = _make_channel(db, youtube_id="UCcarol")
    _make_video(
        db,
        ch,
        privacy="public",
        bytes_stored=GB,
        synced_at=now - timedelta(days=10),
        youtube_id="vid-open",
    )
    _make_video(
        db,
        ch,
        privacy="private",
        bytes_stored=2 * GB,
        synced_at=now - timedelta(days=10),
        youtube_id="vid-sealed",
    )
    db.add(
        UserChannelSubscription(
            user_id=user.id,
            channel_id=ch.id,
            subscribed_at=now - timedelta(days=10),
            unsubscribed_at=now + timedelta(hours=6),
        )
    )
    db.add(
        ChannelOwnership(
            user_id=user.id,
            channel_id=ch.id,
            google_user_id="g-carol",
            authenticated_at=now - timedelta(days=10),
        )
    )
    db.flush()

    total = compute_user_byte_hours_v2(db, user.id, now, now + timedelta(hours=24))
    # (1 GB open + 2 GB sealed) x 6 hours. The 18 hours of 30-day grace
    # that follow are on us.
    assert abs(total - 3 * GB * 6) < 1.0


def test_revoke_then_reauthenticate_is_not_double_billed(db):
    """ensure_ownership() clears revoked_at when the user
    re-authenticates. The sealed window is bounded by storage, not by
    the revoke columns, so a revoke/re-auth round trip inside one
    period bills the same as never having revoked at all."""
    now = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    start, end = now, now + timedelta(hours=24)

    quiet, _ch_q, _own_q = _owner_setup(db, now, username="quiet")
    baseline = compute_user_byte_hours_v2(db, quiet.id, start, end)

    churny, _ch_c, own_c = _owner_setup(db, now, username="churny")
    # Revoked at +4h...
    own_c.revoked_at = now + timedelta(hours=4)
    own_c.user_revoked_at = now + timedelta(hours=4)
    db.flush()
    # ...then re-authenticated at +9h. ensure_ownership() reactivates
    # the same row in place, leaving authenticated_at alone, so there is
    # never a second window to bill.
    own_c.revoked_at = None
    own_c.user_revoked_at = None
    db.flush()

    churned = compute_user_byte_hours_v2(db, churny.id, start, end)
    assert abs(churned - baseline) < 1.0
    assert abs(churned - GB * 24) < 1.0
