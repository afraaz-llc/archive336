"""The Comments scope's data source: GET /api/youtube/comments/search.

The per-channel search has existed since comments were first archived;
this is the cross-channel one behind the Comments toggle on the YouTube
page. Everything it must never do is a leak: another user's comments, a
channel the caller removed, or a channel id they simply asked for but do
not own.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import User, UserChannel, UserChannelVideo, VideoComment
from app.routes.youtube import search_all_comments

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


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


def _channel(db, user, channel_id, name, *, removed=False):
    db.add(
        UserChannel(
            user_id=user.id,
            channel_id=channel_id,
            data_json=f'{{"name": "{name}"}}',
            removed_at=NOW if removed else None,
        )
    )
    db.flush()


def _video(db, user, channel_id, video_id, title):
    db.add(
        UserChannelVideo(
            user_id=user.id,
            channel_id=channel_id,
            video_id=video_id,
            data_json=f'{{"title": "{title}"}}',
        )
    )
    db.flush()


def _comment(
    db,
    user,
    channel_id,
    video_id,
    cid,
    *,
    text="hello",
    likes=0,
    published=NOW,
    deleted=None,
):
    db.add(
        VideoComment(
            id=cid,
            user_id=user.id,
            channel_id=channel_id,
            video_id=video_id,
            author="somebody",
            text=text,
            text_hash=cid,
            like_count=likes,
            published_at=published,
            first_seen_at=NOW,
            last_seen_at=NOW,
            deleted_at=deleted,
        )
    )
    db.flush()


def _search(db, user, **kw):
    kw.setdefault("q", "")
    kw.setdefault("sort", "new")
    kw.setdefault("direction", "desc")
    kw.setdefault("channel_ids", "")
    kw.setdefault("only_deleted", False)
    kw.setdefault("include_deleted", True)
    kw.setdefault("min_likes", 0)
    kw.setdefault("limit", 50)
    kw.setdefault("offset", 0)
    return search_all_comments(db=db, current=user, **kw)


def _two_channels(db):
    u = _user(db, "u1")
    _channel(db, u, "UCaaa", "Alpha")
    _channel(db, u, "UCbbb", "Beta")
    _video(db, u, "UCaaa", "v1", "First video")
    _video(db, u, "UCbbb", "v2", "Second video")
    _comment(db, u, "UCaaa", "v1", "c1", text="alpha comment")
    _comment(db, u, "UCbbb", "v2", "c2", text="beta comment")
    return u


def test_lists_comments_from_every_live_channel(db):
    u = _two_channels(db)
    body = _search(db, u)
    assert body["total"] == 2
    assert {c["id"] for c in body["comments"]} == {"c1", "c2"}


def test_each_row_carries_channel_and_video_attribution(db):
    u = _two_channels(db)
    rows = {c["id"]: c for c in _search(db, u)["comments"]}
    assert rows["c1"]["channelId"] == "UCaaa"
    assert rows["c1"]["channelName"] == "Alpha"
    assert rows["c1"]["videoTitle"] == "First video"
    assert rows["c2"]["channelName"] == "Beta"
    assert rows["c2"]["videoTitle"] == "Second video"


def test_a_removed_channels_comments_are_hidden_not_deleted(db):
    u = _two_channels(db)
    db.query(UserChannel).filter(UserChannel.channel_id == "UCbbb").update(
        {"removed_at": NOW}
    )
    db.flush()

    body = _search(db, u)
    assert [c["id"] for c in body["comments"]] == ["c1"]
    assert body["total"] == 1
    # The row itself survives: removal hides, it does not delete.
    assert db.query(VideoComment).filter(VideoComment.id == "c2").count() == 1


def test_channel_ids_narrows_the_scope(db):
    u = _two_channels(db)
    body = _search(db, u, channel_ids="UCbbb")
    assert [c["id"] for c in body["comments"]] == ["c2"]


def test_asking_for_a_channel_you_do_not_own_returns_nothing(db):
    u = _two_channels(db)
    other = _user(db, "u2")
    _channel(db, other, "UCzzz", "Theirs")
    _video(db, other, "UCzzz", "v9", "Theirs")
    _comment(db, other, "UCzzz", "v9", "c9", text="secret")

    body = _search(db, u, channel_ids="UCzzz")
    assert body["total"] == 0
    assert body["comments"] == []


def test_another_users_comments_never_appear(db):
    u = _two_channels(db)
    other = _user(db, "u2")
    # Same channel id in both archives - the shared-pool case.
    _channel(db, other, "UCaaa", "Alpha")
    _comment(db, other, "UCaaa", "v1", "c-other", text="theirs")

    ids = {c["id"] for c in _search(db, u)["comments"]}
    assert "c-other" not in ids


def test_text_search_is_case_insensitive(db):
    u = _two_channels(db)
    body = _search(db, u, q="ALPHA")
    assert [c["id"] for c in body["comments"]] == ["c1"]


def test_only_deleted_filters_to_deletions(db):
    u = _two_channels(db)
    db.query(VideoComment).filter(VideoComment.id == "c2").update(
        {"deleted_at": NOW}
    )
    db.flush()

    body = _search(db, u, only_deleted=True)
    assert [c["id"] for c in body["comments"]] == ["c2"]
    assert body["comments"][0]["deletedAt"] is not None


def test_sorting_by_deletion_date_implies_deleted_only(db):
    u = _two_channels(db)
    db.query(VideoComment).filter(VideoComment.id == "c2").update(
        {"deleted_at": NOW}
    )
    db.flush()

    body = _search(db, u, sort="deleted")
    assert [c["id"] for c in body["comments"]] == ["c2"]


def test_include_deleted_false_hides_deletions(db):
    u = _two_channels(db)
    db.query(VideoComment).filter(VideoComment.id == "c2").update(
        {"deleted_at": NOW}
    )
    db.flush()

    body = _search(db, u, include_deleted=False)
    assert [c["id"] for c in body["comments"]] == ["c1"]


def test_min_likes_is_a_floor(db):
    u = _two_channels(db)
    db.query(VideoComment).filter(VideoComment.id == "c1").update({"like_count": 5})
    db.flush()

    assert [c["id"] for c in _search(db, u, min_likes=5)["comments"]] == ["c1"]
    assert _search(db, u, min_likes=6)["total"] == 0


def test_sort_by_likes_respects_direction(db):
    u = _two_channels(db)
    db.query(VideoComment).filter(VideoComment.id == "c1").update({"like_count": 9})
    db.flush()

    desc = _search(db, u, sort="top")["comments"]
    assert [c["id"] for c in desc] == ["c1", "c2"]
    asc = _search(db, u, sort="top", direction="asc")["comments"]
    assert [c["id"] for c in asc] == ["c2", "c1"]


def test_newest_first_by_default(db):
    u = _two_channels(db)
    db.query(VideoComment).filter(VideoComment.id == "c1").update(
        {"published_at": NOW - timedelta(days=3)}
    )
    db.flush()

    assert [c["id"] for c in _search(db, u)["comments"]] == ["c2", "c1"]


def test_paging_never_repeats_or_skips_a_tied_row(db):
    """Every comment posted in the same second is a tie; without a
    unique tiebreaker the second page can re-serve the first page's
    rows and drop others entirely."""
    u = _user(db, "u1")
    _channel(db, u, "UCaaa", "Alpha")
    _video(db, u, "UCaaa", "v1", "First video")
    for i in range(10):
        _comment(db, u, "UCaaa", "v1", f"c{i:02d}", published=NOW)

    first = _search(db, u, limit=4, offset=0)
    second = _search(db, u, limit=4, offset=4)
    third = _search(db, u, limit=4, offset=8)

    seen = [c["id"] for c in first["comments"] + second["comments"] + third["comments"]]
    assert first["total"] == 10
    assert len(seen) == 10
    assert len(set(seen)) == 10


def test_a_placeholder_title_is_not_passed_off_as_a_title(db):
    u = _user(db, "u1")
    _channel(db, u, "UCaaa", "Alpha")
    _video(db, u, "UCaaa", "v1", "NA")
    _comment(db, u, "UCaaa", "v1", "c1")

    assert _search(db, u)["comments"][0]["videoTitle"] == ""


def test_a_comment_on_an_unknown_video_still_lists(db):
    u = _user(db, "u1")
    _channel(db, u, "UCaaa", "Alpha")
    _comment(db, u, "UCaaa", "v-missing", "c1")

    body = _search(db, u)
    assert body["total"] == 1
    assert body["comments"][0]["videoTitle"] == ""


def test_a_user_with_no_channels_gets_an_empty_page(db):
    u = _user(db, "u1")
    body = _search(db, u)
    assert body == {"total": 0, "limit": 50, "offset": 0, "comments": []}
