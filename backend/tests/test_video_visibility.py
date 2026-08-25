"""Who can see which videos in a shared-pool channel.

Found while the owner was walking the add-channel flow as their own
customer: GET /channels/{id}/videos checked that the caller had an active
subscription and then returned EVERY Video row for the channel. Discovery
is shared - one Video row per real video, reused by every subscriber - so
any stranger who tracked the same channel would see the owner's private
video titles. access.py
was written for exactly this rule and had zero callers.

Nothing had leaked, but that was luck rather than design, so these tests
pin the rule.

The subtle case is the last one. ``visibility`` is stamped at capture and
frozen, so a video captured while public stays "open" after the creator
privates it - deliberately, because we keep what we captured. That promise
only means something when there is a captured file. A row with no r2_key
is not an archive, it is a title we scraped while the video was public, of
a video that is private today.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import access
import json

from app.models import (
    ChannelOwnership,
    User,
    UserChannelSubscription,
    UserChannelVideo,
    Video,
)
from app import archive


def _user(db, uid):
    u = User(
        id=uid,
        username=uid,
        email=f"{uid}@example.com",
        password_hash="$2b$12$placeholder",
    )
    db.add(u)
    db.flush()
    return u


def _subscribe(db, user, channel, *, unsubscribed_at=None):
    row = UserChannelSubscription(
        user_id=user.id, channel_id=channel.id, unsubscribed_at=unsubscribed_at
    )
    db.add(row)
    db.flush()
    return row


def _own(db, user, channel, *, revoked_at=None):
    row = ChannelOwnership(
        user_id=user.id,
        channel_id=channel.id,
        google_user_id="worker",
        revoked_at=revoked_at,
    )
    db.add(row)
    db.flush()
    return row


def _video(db, channel, title, *, visibility, privacy_current, r2_key=None):
    v = Video(
        channel_id=channel.id,
        youtube_id=f"vid-{title.replace(' ', '-')}",
        title=title,
        published_at=datetime.now(timezone.utc),
        privacy_at_discovery="public" if visibility == "open" else "private",
        privacy_current=privacy_current,
        visibility=visibility,
        r2_key=r2_key,
        bytes_stored=1024 if r2_key else None,
    )
    db.add(v)
    db.flush()
    return v


def _visible(db, user, channel):
    """Titles this user may see, via the same filter the route uses."""
    rows = (
        db.query(Video)
        .filter(Video.channel_id == channel.id)
        .filter(access.visible_video_filter(db, user.id, channel.id))
        .all()
    )
    return {v.title for v in rows}


def test_stranger_cannot_see_sealed_videos(db):
    """The actual bug. A second subscriber to the same pooled channel
    must not see the owner's private videos."""
    owner = _user(db, "u-owner")
    stranger = _user(db, "u-stranger")
    ch = archive.ensure_channel(db, "UCshared")

    _own(db, owner, ch)
    _subscribe(db, owner, ch)
    _subscribe(db, stranger, ch)

    _video(db, ch, "public talk", visibility="open", privacy_current="public")
    _video(db, ch, "family wedding", visibility="sealed", privacy_current="private")

    assert _visible(db, stranger, ch) == {"public talk"}
    assert _visible(db, owner, ch) == {"public talk", "family wedding"}


def test_uncaptured_but_private_now_is_owner_only(db):
    """Captured-while-public + privated-since, with NO file.

    'open' is frozen at capture so subscribers keep what we captured -
    but there is nothing captured here. Showing the title leaks the
    creator's current privacy and returns nothing archival.
    """
    owner = _user(db, "u-o2")
    stranger = _user(db, "u-s2")
    ch = archive.ensure_channel(db, "UCprivated")
    _own(db, owner, ch)
    _subscribe(db, owner, ch)
    _subscribe(db, stranger, ch)

    _video(
        db, ch, "privated later",
        visibility="open", privacy_current="private", r2_key=None,
    )

    assert _visible(db, stranger, ch) == set()
    assert _visible(db, owner, ch) == {"privated later"}


def _captured(db, user, channel_yt, video):
    """Give this user their own archive row for a video."""
    row = UserChannelVideo(
        user_id=user.id,
        channel_id=channel_yt,
        video_id=video.youtube_id,
        data_json=json.dumps({"status": "archived"}),
    )
    db.add(row)
    db.flush()
    return row


def test_captured_then_privated_stays_visible_to_the_capturer(db):
    """The other half of the rule, and the product promise: a user who
    DID archive it while it was public keeps it after it goes private."""
    keeper = _user(db, "u-s3")
    ch = archive.ensure_channel(db, "UCkept")
    _subscribe(db, keeper, ch)

    v = _video(
        db, ch, "archived while public",
        visibility="open", privacy_current="private",
        r2_key="users/x/videos/y/video.mp4",
    )
    _captured(db, keeper, "UCkept", v)

    assert _visible(db, keeper, ch) == {"archived while public"}


def test_archiving_does_not_reopen_the_leak(db):
    """Regression for a bug in the FIRST version of this filter.

    It allowed any row with a non-null r2_key through, but r2_key lives
    on the shared pool row and describes whichever single subscriber
    archived the file - not the caller. So a private-now video stayed
    hidden only until somebody archived it, and then reappeared for
    every subscriber. A probe caught the stranger going from seeing
    nothing to seeing the private title the moment the owner synced.
    """
    owner = _user(db, "u-o4")
    stranger = _user(db, "u-s4")
    ch = archive.ensure_channel(db, "UCreopen")
    _own(db, owner, ch)
    _subscribe(db, owner, ch)
    _subscribe(db, stranger, ch)

    v = _video(
        db, ch, "SECRET",
        visibility="open", privacy_current="private", r2_key=None,
    )
    assert _visible(db, stranger, ch) == set()

    # The owner archives it. The file now exists - but it is the OWNER's
    # capture, not the stranger's.
    v.r2_key = "users/owner/videos/SECRET/video.mp4"
    v.bytes_stored = 999
    _captured(db, owner, "UCreopen", v)
    db.flush()

    assert _visible(db, stranger, ch) == set(), "archiving must not re-expose it"
    assert _visible(db, owner, ch) == {"SECRET"}


def test_no_subscription_sees_nothing(db):
    user = _user(db, "u-none")
    ch = archive.ensure_channel(db, "UCunsub")
    _video(db, ch, "public talk", visibility="open", privacy_current="public")

    assert _visible(db, user, ch) == set()


def test_revoked_owner_loses_sealed_access_after_grace(db):
    """Ownership carries a 30-day grace, then sealed access ends."""
    user = _user(db, "u-revoked")
    ch = archive.ensure_channel(db, "UCrevoked")
    _subscribe(db, user, ch)
    _own(db, user, ch, revoked_at=datetime.now(timezone.utc) - timedelta(days=31))

    _video(db, ch, "sealed one", visibility="sealed", privacy_current="private")
    _video(db, ch, "open one", visibility="open", privacy_current="public")

    assert _visible(db, user, ch) == {"open one"}


def test_revoking_hides_sealed_titles_immediately(db):
    """Disconnecting the worker takes effect at once for sealed titles.

    Ownership normally carries a 30-day grace so a user who reconnects
    never sees a gap. Sealed titles are the exception, at the owner's
    request: we only know those videos exist because an authenticated
    worker enumerated them, so once that authorization is withdrawn the
    site cannot verify the person asking is the owner and should not keep
    displaying them for another month.

    Anything they actually archived is unaffected - see
    test_captured_then_privated_stays_visible_to_the_capturer.
    """
    user = _user(db, "u-grace")
    ch = archive.ensure_channel(db, "UCgrace")
    _subscribe(db, user, ch)
    _own(db, user, ch, revoked_at=datetime.now(timezone.utc) - timedelta(days=3))

    _video(db, ch, "sealed one", visibility="sealed", privacy_current="private")
    _video(db, ch, "open one", visibility="open", privacy_current="public")

    assert _visible(db, user, ch) == {"open one"}
