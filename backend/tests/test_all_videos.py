"""The cross-channel video library: GET /api/youtube/videos.

This endpoint lists every video the caller may see across all of their
channels at once. That makes it the second listing endpoint in the
codebase, and the first one leaked the owner's private video titles to
any stranger who happened to track the same pooled channel. So the test
that matters most here is not "does it return videos" - it is "does a
stranger still get nothing", asked of the endpoint rather than of the
filter it is built on.

It composes ``visible_video_filter`` per channel and ORs the clauses.
These tests pin that composition: the per-channel rule is already
covered in test_video_visibility, so what is checked here is that
spanning channels does not weaken it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import archive
from app.models import (
    ChannelOwnership,
    User,
    UserChannelSubscription,
    UserChannelVideo,
    Video,
)
from app.routes.youtube import list_all_videos


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
    db.add(
        UserChannelSubscription(
            user_id=user.id,
            channel_id=channel.id,
            unsubscribed_at=unsubscribed_at,
        )
    )
    db.flush()


def _own(db, user, channel):
    db.add(
        ChannelOwnership(
            user_id=user.id,
            channel_id=channel.id,
            google_user_id="worker",
            revoked_at=None,
        )
    )
    db.flush()


def _video(db, channel, title, *, visibility="open", privacy="public", days_ago=0):
    v = Video(
        channel_id=channel.id,
        youtube_id=f"vid-{title.replace(' ', '-')}",
        title=title,
        published_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        privacy_at_discovery="public" if visibility == "open" else "private",
        privacy_current=privacy,
        visibility=visibility,
        r2_key=None,
        bytes_stored=None,
    )
    db.add(v)
    db.flush()
    return v


def _titles(db, user, **kw):
    out = list_all_videos(cursor=None, limit=500, db=db, current=user, **kw)
    return {i["title"] for i in out["items"]}


def test_stranger_gets_no_sealed_videos_from_the_library(db):
    """The regression this endpoint could have reintroduced.

    Spanning every channel at once must not become a way around the
    per-channel rule.
    """
    owner = _user(db, "u-owner")
    stranger = _user(db, "u-stranger")
    ch = archive.ensure_channel(db, "UCshared", title="Shared")

    _own(db, owner, ch)
    _subscribe(db, owner, ch)
    _subscribe(db, stranger, ch)

    _video(db, ch, "public talk")
    _video(db, ch, "private thing", visibility="sealed", privacy="private")

    assert _titles(db, stranger) == {"public talk"}
    assert _titles(db, owner) == {"public talk", "private thing"}


def test_library_spans_every_subscribed_channel(db):
    u = _user(db, "u1")
    a = archive.ensure_channel(db, "UCaaa", title="Alpha")
    b = archive.ensure_channel(db, "UCbbb", title="Beta")
    _subscribe(db, u, a)
    _subscribe(db, u, b)
    _video(db, a, "from alpha")
    _video(db, b, "from beta")

    assert _titles(db, u) == {"from alpha", "from beta"}


def test_unsubscribed_channels_drop_out(db):
    u = _user(db, "u1")
    a = archive.ensure_channel(db, "UCaaa", title="Alpha")
    b = archive.ensure_channel(db, "UCbbb", title="Beta")
    _subscribe(db, u, a)
    _subscribe(db, u, b, unsubscribed_at=datetime.now(timezone.utc))
    _video(db, a, "kept")
    _video(db, b, "dropped")

    assert _titles(db, u) == {"kept"}


def test_no_subscriptions_is_an_empty_page_not_an_error(db):
    u = _user(db, "u1")
    assert list_all_videos(cursor=None, limit=50, db=db, current=u) == {
        "items": [],
        "nextCursor": None,
    }


def test_each_row_says_which_channel_it_came_from(db):
    """A mixed list is unreadable without attribution, so the payload
    carries it rather than making the client join against the channel
    list it happens to have loaded."""
    u = _user(db, "u1")
    a = archive.ensure_channel(db, "UCaaa", title="Alpha", handle="@alpha")
    _subscribe(db, u, a)
    _video(db, a, "one")

    item = list_all_videos(cursor=None, limit=50, db=db, current=u)["items"][0]
    assert item["channelId"] == "UCaaa"
    assert item["channelName"] == "Alpha"
    assert item["channelHandle"] == "@alpha"


def test_sorted_newest_first_across_channels_not_grouped_by_channel(db):
    """Interleaving is the point. Sorting per channel and concatenating
    would put all of Alpha above all of Beta regardless of date."""
    u = _user(db, "u1")
    a = archive.ensure_channel(db, "UCaaa", title="Alpha")
    b = archive.ensure_channel(db, "UCbbb", title="Beta")
    _subscribe(db, u, a)
    _subscribe(db, u, b)
    _video(db, a, "oldest", days_ago=30)
    _video(db, b, "middle", days_ago=15)
    _video(db, a, "newest", days_ago=1)

    out = list_all_videos(cursor=None, limit=50, db=db, current=u)
    assert [i["title"] for i in out["items"]] == ["newest", "middle", "oldest"]


def test_cursor_walks_the_whole_library_without_gaps_or_repeats(db):
    u = _user(db, "u1")
    a = archive.ensure_channel(db, "UCaaa", title="Alpha")
    b = archive.ensure_channel(db, "UCbbb", title="Beta")
    _subscribe(db, u, a)
    _subscribe(db, u, b)
    for i in range(9):
        _video(db, a if i % 2 else b, f"v{i:02d}", days_ago=i)

    seen, cursor, pages = [], None, 0
    while True:
        out = list_all_videos(cursor=cursor, limit=2, db=db, current=u)
        seen.extend(i["title"] for i in out["items"])
        cursor = out["nextCursor"]
        pages += 1
        if cursor is None or pages > 20:
            break

    assert pages == 5, "9 rows at 2 per page"
    assert len(seen) == len(set(seen)) == 9, "no repeats, no gaps"
    assert seen == sorted(seen), "paging preserved the newest-first order"


def test_archive_state_is_the_callers_own_not_another_subscribers(db):
    """Discovery is shared but archives are per-user. Reading the pooled
    row's file state would report the owner's archive as the stranger's.
    """
    owner = _user(db, "u-owner")
    stranger = _user(db, "u-stranger")
    ch = archive.ensure_channel(db, "UCshared", title="Shared")
    _subscribe(db, owner, ch)
    _subscribe(db, stranger, ch)
    v = _video(db, ch, "shared video")

    db.add(
        UserChannelVideo(
            user_id=owner.id,
            channel_id=ch.youtube_id,
            video_id=v.youtube_id,
            data_json='{"status": "archived", "fileSizeBytes": 4096}',
        )
    )
    db.flush()

    mine = list_all_videos(cursor=None, limit=50, db=db, current=owner)["items"][0]
    theirs = list_all_videos(cursor=None, limit=50, db=db, current=stranger)["items"][0]

    assert mine["status"] == "archived" and mine["fileSizeBytes"] == 4096
    assert theirs["status"] == "discovered" and theirs["fileSizeBytes"] == 0


def test_a_video_that_failed_before_it_had_a_row_still_reads_as_failed(db):
    """The home page counts failures from SyncJob rows; the listing used
    to report whatever the user's own blob said. A video whose FIRST
    attempt fails never gets a blob, so the banner counted it and the
    list could not show it - "3 videos failed" linking to 2 rows."""
    from app.models import SyncJob

    u = _user(db, "u1")
    ch = archive.ensure_channel(db, "UCaaa", title="Alpha")
    _subscribe(db, u, ch)
    v = _video(db, ch, "never even started")

    db.add(
        SyncJob(
            user_id=u.id,
            channel_id=ch.youtube_id,
            video_id=v.youtube_id,
            kind="video",
            status="failed",
            error="Video unavailable. This video is private",
        )
    )
    db.flush()

    item = list_all_videos(cursor=None, limit=50, db=db, current=u)["items"][0]
    assert item["status"] == "failed", "no UserChannelVideo row, but still failed"


def test_a_failure_that_was_retried_and_stored_is_not_still_failed(db):
    """Otherwise the count never goes down: a video that failed once and
    then archived would sit in the banner forever."""
    from app.models import SyncJob

    u = _user(db, "u1")
    ch = archive.ensure_channel(db, "UCaaa", title="Alpha")
    _subscribe(db, u, ch)
    v = _video(db, ch, "failed then worked")

    db.add(
        SyncJob(
            user_id=u.id,
            channel_id=ch.youtube_id,
            video_id=v.youtube_id,
            kind="video",
            status="failed",
            error="timed out",
        )
    )
    db.add(
        UserChannelVideo(
            user_id=u.id,
            channel_id=ch.youtube_id,
            video_id=v.youtube_id,
            data_json='{"status": "archived", "fileSizeBytes": 512}',
        )
    )
    db.flush()

    item = list_all_videos(cursor=None, limit=50, db=db, current=u)["items"][0]
    assert item["status"] == "archived"


def test_a_failure_currently_being_retried_is_not_reported_as_failed(db):
    """Mid-retry is not something the user needs to look at."""
    from app.models import SyncJob

    u = _user(db, "u1")
    ch = archive.ensure_channel(db, "UCaaa", title="Alpha")
    _subscribe(db, u, ch)
    v = _video(db, ch, "retrying now")

    for state in ("failed", "pending"):
        db.add(
            SyncJob(
                user_id=u.id,
                channel_id=ch.youtube_id,
                video_id=v.youtube_id,
                kind="video",
                status=state,
                error="timed out" if state == "failed" else None,
            )
        )
    db.flush()

    item = list_all_videos(cursor=None, limit=50, db=db, current=u)["items"][0]
    assert item["status"] != "failed"


def test_a_forgiven_failure_stops_reading_as_failed(db):
    """The count and the list must agree in both directions. An unaired
    scheduled stream is dropped from the failure count, so a stale
    "failed" in the user's own blob must not keep showing it as one -
    that put 3 rows behind a banner that said 2."""
    from app.models import SyncJob

    u = _user(db, "u1")
    ch = archive.ensure_channel(db, "UCaaa", title="Alpha")
    _subscribe(db, u, ch)
    v = _video(db, ch, "not aired yet")

    db.add(
        SyncJob(
            user_id=u.id, channel_id=ch.youtube_id, video_id=v.youtube_id,
            kind="video", status="failed",
            error="ERROR: [youtube] x: This live event will begin in 3 hours.",
        )
    )
    db.add(
        UserChannelVideo(
            user_id=u.id, channel_id=ch.youtube_id, video_id=v.youtube_id,
            data_json='{"status": "failed"}',
        )
    )
    db.flush()

    item = list_all_videos(cursor=None, limit=50, db=db, current=u)["items"][0]
    assert item["status"] == "discovered", "known, not held, not a failure"


def test_a_video_with_no_metadata_still_gets_its_required_collections(db):
    """The frontend types tags/comments/captionLanguages as required
    arrays, but they come out of metadata_json - so a video that never
    synced had no metadata and the keys were absent, arriving as
    undefined. Nothing linked to such a video until the library made
    every row clickable, at which point opening one threw "Cannot read
    properties of undefined (reading 'length')"."""
    u = _user(db, "u1")
    ch = archive.ensure_channel(db, "UCaaa", title="Alpha")
    _subscribe(db, u, ch)
    v = archive.ensure_placeholder_video(
        db, channel=ch, youtube_video_id="v1", title="never synced",
        privacy="public",
    )
    assert v.metadata_json is None, "the case that produced the crash"

    item = list_all_videos(cursor=None, limit=50, db=db, current=u)["items"][0]

    assert item["tags"] == []
    assert item["comments"] == []
    assert item["captionLanguages"] == []
    assert item["commentCount"] == 0
    assert item["viewCount"] == 0
    assert item["type"] == "video"


def test_real_metadata_still_wins_over_the_defaults(db):
    import json as _json

    u = _user(db, "u1")
    ch = archive.ensure_channel(db, "UCaaa", title="Alpha")
    _subscribe(db, u, ch)
    v = _video(db, ch, "has metadata")
    v.metadata_json = _json.dumps(
        {"tags": ["a", "b"], "captionLanguages": ["en"], "viewCount": 42}
    )
    db.flush()

    item = list_all_videos(cursor=None, limit=50, db=db, current=u)["items"][0]

    assert item["tags"] == ["a", "b"]
    assert item["captionLanguages"] == ["en"]
    assert item["viewCount"] == 42
