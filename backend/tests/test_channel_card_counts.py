"""The "X / Y" on a channel card has to mean what the channel page means.

The owner opened Le Frog and read three numbers for one channel: the
page header said 3 archived, the card on the YouTube page said 2 / 4,
and a saved filter chip said 4. The header was right - three files,
253.2 MB of them.

The card was counting the word "archived" in each row's status rather
than asking whether we hold the file. One of those three videos had been
taken down on YouTube, which overwrites the status with
deleted_on_youtube, so the card stopped counting a video we still have.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app import archive
from app.models import (
    User,
    UserChannel,
    UserChannelSubscription,
    UserChannelVideo,
    Video,
)
from app.routes.youtube import list_channels

NOW = datetime(2026, 9, 25, tzinfo=timezone.utc)


def _setup(db, videos):
    u = User(
        id="u1", username="u1", email="u1@x.com",
        password_hash="p", payment_status="active",
    )
    db.add(u)
    db.flush()
    ch = archive.ensure_channel(db, "UCme", title="Le Frog")
    db.add(UserChannelSubscription(user_id=u.id, channel_id=ch.id))
    db.add(
        UserChannel(
            user_id=u.id, channel_id="UCme",
            data_json=json.dumps({"id": "UCme", "settings": {"active": True}}),
        )
    )
    for vid, blob in videos:
        db.add(
            Video(
                channel_id=ch.id, youtube_id=vid, title=vid,
                published_at=NOW, privacy_at_discovery="public",
                privacy_current="public", visibility="open",
                r2_key="k" if blob.get("localPath") else None,
            )
        )
        db.add(
            UserChannelVideo(
                user_id=u.id, channel_id="UCme", video_id=vid,
                data_json=json.dumps(blob),
            )
        )
    db.flush()
    return u


def _card(db, user):
    cards = list_channels(db=db, current=user)
    assert len(cards) == 1
    return cards[0]


def test_a_video_we_hold_counts_even_after_youtube_removes_it(db):
    u = _setup(db, [
        ("held", {"status": "archived", "localPath": "users/u1/a.mp4"}),
        ("gone-but-held", {
            "status": "deleted_on_youtube", "localPath": "users/u1/b.mp4",
        }),
    ])
    card = _card(db, u)
    assert card["archivedVideoCount"] == 2


def test_a_video_gone_before_we_got_it_leaves_the_total(db):
    """It can never be archived, so counting it in the denominator pins
    the card one short forever."""
    u = _setup(db, [
        ("held", {"status": "archived", "localPath": "users/u1/a.mp4"}),
        ("never-had-it", {"status": "deleted_on_youtube", "localPath": None}),
    ])
    card = _card(db, u)
    assert card["archivedVideoCount"] == 1
    assert card["knownVideoCount"] == 1


def test_the_le_frog_case(db):
    """Three files held, one of them removed from YouTube afterwards, plus
    a stream that went before we ever captured it. The card read 2 / 4."""
    u = _setup(db, [
        ("a", {"status": "archived", "localPath": "users/u1/a.mp4"}),
        ("b", {"status": "archived", "localPath": "users/u1/b.mp4"}),
        ("c", {"status": "deleted_on_youtube", "localPath": "users/u1/c.mp4"}),
        ("stream", {"status": "deleted_on_youtube", "localPath": None}),
    ])
    card = _card(db, u)
    assert (card["archivedVideoCount"], card["knownVideoCount"]) == (3, 3)


def test_a_pending_video_still_counts_towards_the_total(db):
    """Not yet downloaded is not unarchivable - it is the whole reason
    the ratio has a denominator."""
    u = _setup(db, [
        ("held", {"status": "archived", "localPath": "users/u1/a.mp4"}),
        ("waiting", {"status": "discovered", "localPath": None}),
    ])
    card = _card(db, u)
    assert (card["archivedVideoCount"], card["knownVideoCount"]) == (1, 2)
