"""Unit tests for app.access — one privacy tier per test.

Each test sets up the minimum graph required to exercise a single
rule, then asserts the access helper returns True or False as
appropriate. Where a rule has multiple states (e.g. revoked
ownership inside vs outside the grace window), each gets its own
assertion in the same test.

We don't test combinations across tiers in this file — those are
exercised through the integration paths (billing recompute, route
gates). This file is purely "does the rule for tier X work."
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import access
from app.models import (
    visibility_for_privacy,
)  # noqa: F401
from app.models import (
    Channel,
    ChannelMembership,
    ChannelOwnership,
    UserChannelSubscription,
    User,
    Video,
)


# ---- Builders -------------------------------------------------------

# Tiny helpers that return persisted objects. Keeps each test
# focused on the rule under exam rather than SQLAlchemy ceremony.


def _make_user(db, *, username: str = "alice") -> User:
    user = User(
        id=f"user-{username}",
        username=username,
        email=f"{username}@example.com",
        password_hash="$2b$12$placeholder",
    )
    db.add(user)
    db.flush()
    return user


def _make_channel(db, *, youtube_id: str = "UCabc") -> Channel:
    ch = Channel(
        id=f"ch-{youtube_id}",
        youtube_id=youtube_id,
        handle=f"@{youtube_id}",
        title=f"{youtube_id} channel",
    )
    db.add(ch)
    db.flush()
    return ch


def _make_video(
    db,
    channel: Channel,
    *,
    privacy: str,
    youtube_id: str = "vidA",
) -> Video:
    v = Video(
        id=f"v-{youtube_id}",
        channel_id=channel.id,
        youtube_id=youtube_id,
        title=f"{youtube_id} title",
        published_at=datetime.now(timezone.utc),
        privacy_at_discovery=privacy,
        privacy_current=privacy,
        # Stamp visibility the way production does (archive.py does this
        # on every Video it creates). Without it every fixture video
        # defaulted to "open", so these tests silently exercised the
        # public path and asserted the private rule against it.
        visibility=visibility_for_privacy(privacy),
        bytes_stored=1_000_000,
        r2_key=f"videos/v-{youtube_id}.mp4",
        synced_at=datetime.now(timezone.utc),
    )
    db.add(v)
    db.flush()
    return v


def _subscribe(
    db,
    user: User,
    channel: Channel,
    *,
    unsubscribed_at=None,
) -> UserChannelSubscription:
    sub = UserChannelSubscription(
        user_id=user.id,
        channel_id=channel.id,
        unsubscribed_at=unsubscribed_at,
    )
    db.add(sub)
    db.flush()
    return sub


def _own(
    db,
    user: User,
    channel: Channel,
    *,
    revoked_at=None,
) -> ChannelOwnership:
    own = ChannelOwnership(
        user_id=user.id,
        channel_id=channel.id,
        google_user_id="google-1",
        revoked_at=revoked_at,
    )
    db.add(own)
    db.flush()
    return own


def _member(
    db,
    user: User,
    channel: Channel,
    *,
    expires_at=None,
    revoked_at=None,
) -> ChannelMembership:
    mem = ChannelMembership(
        user_id=user.id,
        channel_id=channel.id,
        google_user_id="google-1",
        expires_at=expires_at,
        revoked_at=revoked_at,
    )
    db.add(mem)
    db.flush()
    return mem


# ---- Public tier ----------------------------------------------------


def test_public_video_visible_to_subscriber(db):
    user = _make_user(db)
    ch = _make_channel(db)
    video = _make_video(db, ch, privacy="public")
    _subscribe(db, user, ch)

    assert access.can_user_access_video(db, user, video) is True


def test_public_video_hidden_without_subscription(db):
    user = _make_user(db)
    ch = _make_channel(db)
    video = _make_video(db, ch, privacy="public")
    # No subscription -> no access, even for public content.

    assert access.can_user_access_video(db, user, video) is False


def test_public_video_grace_period_after_unsubscribe(db):
    """A user who unsubscribed 5 days ago still has access (grace).
    A user who unsubscribed 40 days ago has lost it."""
    ch = _make_channel(db)
    video = _make_video(db, ch, privacy="public")

    alice = _make_user(db, username="alice")
    _subscribe(
        db,
        alice,
        ch,
        unsubscribed_at=datetime.now(timezone.utc) - timedelta(days=5),
    )
    bob = _make_user(db, username="bob")
    _subscribe(
        db,
        bob,
        ch,
        unsubscribed_at=datetime.now(timezone.utc) - timedelta(days=40),
    )

    assert access.can_user_access_video(db, alice, video) is True
    assert access.can_user_access_video(db, bob, video) is False


# ---- Unlisted tier --------------------------------------------------


def test_unlisted_is_owner_only_not_public(db):
    """Unlisted is link-only on YouTube, so we treat it as sealed.

    This test used to assert the opposite - that unlisted follows the
    public rule - which is what access.py's own docstring still claimed.
    The implementation disagreed with the docstring and the
    implementation is right: an unlisted video is one the creator chose
    to keep out of search, and handing it to every subscriber of a shared
    channel is precisely the over-exposure this module exists to prevent.
    See visibility_for_privacy() in models.py.
    """
    user = _make_user(db)
    ch = _make_channel(db)
    video = _make_video(db, ch, privacy="unlisted")
    _subscribe(db, user, ch)

    assert access.can_user_access_video(db, user, video) is False

    _own(db, user, ch)
    assert access.can_user_access_video(db, user, video) is True


# ---- Age-restricted tier --------------------------------------------


def test_age_restricted_video_same_rules_as_public(db):
    """Age-restricted is functionally public for redistribution; the
    age check happens at sync time on the worker side, not at view
    time. Any active subscriber can access."""
    user = _make_user(db)
    ch = _make_channel(db)
    video = _make_video(db, ch, privacy="age_restricted")
    _subscribe(db, user, ch)

    assert access.can_user_access_video(db, user, video) is True


# ---- Members-only tier ----------------------------------------------


def test_members_only_is_owner_only_membership_not_yet_honored(db):
    """Members-only content is sealed: owner-only, and a verified
    membership does NOT unlock it today.

    has_verified_membership() is implemented but nothing calls it, so
    this test pins the rule that actually ships rather than the one the
    docstring describes. The conservative direction is the right one to
    leave standing while the membership tier is unbuilt - the failure
    mode of being too strict is a user not seeing their own content,
    which they can fix by authenticating; the failure mode of being too
    loose is publishing someone's paid-members video to strangers.
    """
    user = _make_user(db)
    ch = _make_channel(db)
    video = _make_video(db, ch, privacy="members_only")
    _subscribe(db, user, ch)

    assert access.can_user_access_video(db, user, video) is False

    # Even a verified, unexpired membership does not grant access.
    _member(
        db,
        user,
        ch,
        expires_at=datetime.now(timezone.utc) + timedelta(days=15),
    )
    assert access.can_user_access_video(db, user, video) is False

    # Ownership does.
    _own(db, user, ch)
    assert access.can_user_access_video(db, user, video) is True


def test_members_only_expired_membership_blocks(db):
    user = _make_user(db)
    ch = _make_channel(db)
    video = _make_video(db, ch, privacy="members_only")
    _subscribe(db, user, ch)
    _member(
        db,
        user,
        ch,
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
    )

    # Expired membership = no access. No grace period on memberships.
    assert access.can_user_access_video(db, user, video) is False


def test_members_only_revoked_membership_blocks(db):
    user = _make_user(db)
    ch = _make_channel(db)
    video = _make_video(db, ch, privacy="members_only")
    _subscribe(db, user, ch)
    _member(
        db,
        user,
        ch,
        revoked_at=datetime.now(timezone.utc),
    )

    assert access.can_user_access_video(db, user, video) is False


# ---- Private tier ---------------------------------------------------


def test_private_video_visible_only_to_owner(db):
    """Only users with an active ChannelOwnership see private content,
    regardless of subscription state."""
    ch = _make_channel(db)
    video = _make_video(db, ch, privacy="private")

    # Subscriber-only: no access.
    alice = _make_user(db, username="alice")
    _subscribe(db, alice, ch)
    assert access.can_user_access_video(db, alice, video) is False

    # Owner (no subscription needed): access.
    bob = _make_user(db, username="bob")
    _own(db, bob, ch)
    assert access.can_user_access_video(db, bob, video) is True

    # Unrelated user: no access.
    carol = _make_user(db, username="carol")
    assert access.can_user_access_video(db, carol, video) is False


def test_private_video_revoked_ownership_grace_period(db):
    """Ownership revoked 5 days ago = still in grace. 40 days ago = lost."""
    ch = _make_channel(db)
    video = _make_video(db, ch, privacy="private")

    alice = _make_user(db, username="alice")
    _own(
        db,
        alice,
        ch,
        revoked_at=datetime.now(timezone.utc) - timedelta(days=5),
    )
    bob = _make_user(db, username="bob")
    _own(
        db,
        bob,
        ch,
        revoked_at=datetime.now(timezone.utc) - timedelta(days=40),
    )

    assert access.can_user_access_video(db, alice, video) is True
    assert access.can_user_access_video(db, bob, video) is False


# ---- Multi-owner team channel --------------------------------------


def test_multiple_owners_each_get_private_access(db):
    """The 3-person YouTube team case: three users all OAuth as the
    same channel, all three see private content independently."""
    ch = _make_channel(db)
    video = _make_video(db, ch, privacy="private")

    owners = [
        _make_user(db, username=f"owner{i}") for i in range(3)
    ]
    for owner in owners:
        _own(db, owner, ch)

    for owner in owners:
        assert access.can_user_access_video(db, owner, video) is True


# ---- Billable-video query -------------------------------------------


def test_billable_video_query_excludes_unsynced(db):
    """Videos with bytes_stored=None or 0 don't count for billing
    even if the user has access to them — we don't bill for what
    we haven't stored."""
    user = _make_user(db)
    ch = _make_channel(db)
    _subscribe(db, user, ch)

    synced = _make_video(db, ch, privacy="public", youtube_id="synced")
    unsynced = Video(
        id="v-unsynced",
        channel_id=ch.id,
        youtube_id="unsynced",
        title="not yet downloaded",
        published_at=datetime.now(timezone.utc),
        privacy_at_discovery="public",
        privacy_current="public",
        bytes_stored=None,
        r2_key=None,
        synced_at=None,
    )
    db.add(unsynced)
    db.flush()

    billable_ids = {v.id for v in access.billable_video_query(db, user.id).all()}
    assert billable_ids == {synced.id}
