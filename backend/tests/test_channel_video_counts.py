"""The "archived / total" ratio on a channel card.

The owner's card read "11 / 9". Both numbers were correct and they were
answers to different questions: the numerator counted every video we had
archived for him, including the seven private ones, and the denominator
was ``videoCount`` - YouTube's public figure, which by definition counts
only what a stranger can see.

Archiving private videos is the whole point of authenticating a channel,
so the moment authentication started working the numerator could exceed
the denominator. It was not a rounding error waiting to be nudged; it was
guaranteed for any channel with private videos.

``knownVideoCount`` answers the numerator's question: videos WE hold a row
for, filtered to what this caller may see. That last part matters as much
as the count - a subscriber who is not allowed to see the owner's private
videos must not learn they exist by reading a denominator that counts
them.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app import access, archive
from app.models import (
    ChannelOwnership,
    User,
    UserChannelSubscription,
    UserChannelVideo,
    Video,
)


def _user(db, uid):
    u = User(
        id=uid, username=uid, email=f"{uid}@x.com", password_hash="p",
    )
    db.add(u)
    db.flush()
    return u


def _video(db, channel, title, *, visibility, privacy_current):
    v = Video(
        channel_id=channel.id,
        youtube_id=f"vid-{title.replace(' ', '-')}",
        title=title,
        published_at=datetime.now(timezone.utc),
        privacy_at_discovery="public" if visibility == "open" else "private",
        privacy_current=privacy_current,
        visibility=visibility,
    )
    db.add(v)
    db.flush()
    return v


def _known(db, user, channel):
    """The denominator, computed exactly as the routes compute it."""
    return (
        db.query(Video)
        .filter(Video.channel_id == channel.id)
        .filter(access.visible_video_filter(db, user.id, channel.id))
        .count()
    )


def _archived_for(db, user, channel_yt):
    """The numerator, computed exactly as list_channels computes it."""
    n = 0
    for row in db.query(UserChannelVideo).filter(
        UserChannelVideo.user_id == user.id,
        UserChannelVideo.channel_id == channel_yt,
    ):
        if (json.loads(row.data_json) or {}).get("status") == "archived":
            n += 1
    return n


def _setup(db):
    owner = _user(db, "u-owner")
    ch = archive.ensure_channel(db, "UCcounts", title="Afraaz")
    db.add(ChannelOwnership(
        user_id=owner.id, channel_id=ch.id, google_user_id="worker",
    ))
    db.add(UserChannelSubscription(user_id=owner.id, channel_id=ch.id))
    db.flush()
    return owner, ch


def test_the_owner_never_sees_more_archived_than_total(db):
    """The actual bug: 3 public + 7 private, all archived, against a
    YouTube videoCount that only ever knew about the 3."""
    owner, ch = _setup(db)
    for i in range(3):
        _video(db, ch, f"public {i}", visibility="open", privacy_current="public")
    for i in range(7):
        _video(db, ch, f"private {i}", visibility="sealed", privacy_current="private")
    for v in db.query(Video).filter(Video.channel_id == ch.id):
        db.add(UserChannelVideo(
            user_id=owner.id, channel_id="UCcounts", video_id=v.youtube_id,
            data_json=json.dumps({"status": "archived"}),
        ))
    db.flush()

    archived = _archived_for(db, owner, "UCcounts")
    known = _known(db, owner, ch)

    assert archived == 10
    assert known == 10, "the denominator counts what we hold, not what YouTube admits"
    assert archived <= known


def test_a_subscriber_denominator_excludes_what_they_cannot_see(db):
    """The denominator must not leak the existence of private videos.

    Counting every row would tell a stranger the channel has 10 videos
    while showing them 3, which is the same disclosure the video list
    filter exists to prevent - just expressed as a number.
    """
    owner, ch = _setup(db)
    stranger = _user(db, "u-stranger")
    db.add(UserChannelSubscription(user_id=stranger.id, channel_id=ch.id))
    for i in range(3):
        _video(db, ch, f"public {i}", visibility="open", privacy_current="public")
    for i in range(7):
        _video(db, ch, f"private {i}", visibility="sealed", privacy_current="private")
    db.flush()

    assert _known(db, owner, ch) == 10
    assert _known(db, stranger, ch) == 3


def test_payload_falls_back_to_videoCount_when_not_computed(db):
    """An un-updated call site degrades to the old number rather than
    rendering "11 / 0"."""
    owner, ch = _setup(db)
    ch.metadata_json = json.dumps({"videoCount": 9})
    sub = (
        db.query(UserChannelSubscription)
        .filter(UserChannelSubscription.user_id == owner.id)
        .one()
    )
    db.flush()

    payload = archive.channel_response_payload(ch, sub)
    assert payload["knownVideoCount"] == 9

    payload = archive.channel_response_payload(ch, sub, known_video_count=10)
    assert payload["knownVideoCount"] == 10
