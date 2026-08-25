"""Validation + safety gating of the worker's comment payload.

These cover the two pure helpers that stand between the worker's completion
payload and the shared comments store engine, with no DB needed - the same
shape as test_worker_metadata_fields.py.

The whole point of the feature's safety rule is that a false "your comment was
deleted" is the worst output it can produce. yt-dlp returns a snapshot, never a
diff, and can exit 0 on a truncated subset. So deletions are only ever
permitted on a fetch the worker certified complete AND that survives a sanity
ratio against yt-dlp's reported comment_count. _comments_allow_deletions is
that gate, and it can only ever turn deletions OFF.
"""
from __future__ import annotations

from app.routes.youtube import (
    _COMMENTS_SANITY_MIN_RATIO,
    _COMMENTS_SANITY_MIN_REPORTED,
    _comments_allow_deletions,
    _worker_comment_items,
)


def _item(**over):
    base = {
        "id": "c1",
        "parentId": None,
        "author": "Alice",
        "authorChannelId": "UCauthor",
        "text": "hello",
        "likeCount": 3,
        "isEdited": False,
        "viewerRatingLike": False,
        "publishedAt": "2026-07-20T10:00:00Z",
        "updatedAt": None,
    }
    base.update(over)
    return base


def _payload(items, **block_over):
    block = {"complete": True, "reportedTotal": len(items), "items": items}
    block.update(block_over)
    return {"comments": block}


# ---------- _worker_comment_items ----------


def test_valid_payload_maps_camelcase_to_engine_keys():
    top = _item(id="top", parentId=None)
    reply = _item(id="top.reply", parentId="top", author="Bob")
    items = _worker_comment_items(_payload([top, reply]))
    assert items is not None
    assert [i["id"] for i in items] == ["top", "top.reply"]
    a, b = items
    assert a["parent_id"] is None
    assert b["parent_id"] == "top"
    assert a["author"] == "Alice"
    assert a["author_channel_id"] == "UCauthor"
    assert a["text"] == "hello"
    assert a["like_count"] == 3
    assert a["published_at"] == "2026-07-20T10:00:00Z"


def test_unexposed_fields_are_hard_defaulted():
    """is_edited / viewer_rating_like / updated_at are things yt-dlp cannot
    expose, so the worker can never assert them - we fix them false/false/null
    regardless of what the payload claims, letting the engine's text-hash diff
    own edit detection."""
    items = _worker_comment_items(
        _payload([_item(isEdited=True, viewerRatingLike=True, updatedAt="x")])
    )
    assert items[0]["is_edited"] is False
    assert items[0]["viewer_rating_like"] is False
    assert items[0]["updated_at"] is None


def test_null_like_count_becomes_zero_and_bool_is_rejected():
    assert _worker_comment_items(_payload([_item(likeCount=None)]))[0][
        "like_count"
    ] == 0
    # bool is an int subclass; True must not survive as a like count of 1.
    assert _worker_comment_items(_payload([_item(likeCount=True)]))[0][
        "like_count"
    ] == 0


def test_empty_parent_id_normalises_to_top_level():
    assert _worker_comment_items(_payload([_item(parentId="")]))[0][
        "parent_id"
    ] is None


def test_item_without_a_string_id_is_dropped():
    m = _item()
    del m["id"]
    assert _worker_comment_items(_payload([m])) == []
    assert _worker_comment_items(_payload([_item(id="")])) == []
    assert _worker_comment_items(_payload([_item(id=123)])) == []
    # non-dict items are skipped, not fatal.
    assert _worker_comment_items(_payload(["nope", _item(id="keep")]))[0][
        "id"
    ] == "keep"


def test_missing_author_or_text_defaults_to_empty_string():
    m = _item()
    del m["author"]
    del m["text"]
    got = _worker_comment_items(_payload([m]))[0]
    assert got["author"] == ""
    assert got["text"] == ""


def test_unusable_payloads_return_none():
    assert _worker_comment_items(None) is None
    assert _worker_comment_items("nope") is None
    assert _worker_comment_items({}) is None  # no comments block
    assert _worker_comment_items({"comments": []}) is None  # not a dict
    assert _worker_comment_items({"comments": {}}) is None  # items missing
    assert (
        _worker_comment_items({"comments": {"items": "no"}}) is None
    )  # items not a list


def test_empty_thread_is_usable_not_none():
    """An empty items list is 'we looked and there were none', which the
    engine short-circuits safely - distinct from an unusable payload."""
    assert _worker_comment_items(_payload([])) == []


# ---------- _comments_allow_deletions (safety guards 2 + 3) ----------


def test_incomplete_fetch_never_permits_deletions():
    assert _comments_allow_deletions(
        complete=False, reported_total=0, fetched_count=100
    ) is False
    # A non-bool complete is distrusted as false.
    assert _comments_allow_deletions(
        complete="true", reported_total=0, fetched_count=100
    ) is False
    assert _comments_allow_deletions(
        complete=1, reported_total=0, fetched_count=100
    ) is False


def test_complete_fetch_with_no_reported_total_is_permitted():
    """When reportedTotal is missing / unusable we cannot judge shortfall, so
    completeness alone stands - we never manufacture a suppression."""
    assert _comments_allow_deletions(
        complete=True, reported_total=None, fetched_count=5
    ) is True
    assert _comments_allow_deletions(
        complete=True, reported_total="lots", fetched_count=5
    ) is True


def test_complete_fetch_below_the_floor_is_permitted():
    """Tiny threads are not gated on reply-count noise: under the floor the
    ratio never bites even when fetched_count is small."""
    below = _COMMENTS_SANITY_MIN_REPORTED - 1
    assert _comments_allow_deletions(
        complete=True, reported_total=below, fetched_count=0
    ) is True


def test_truncated_complete_fetch_is_suppressed():
    """complete=True but implausibly short against reportedTotal: a bot-check
    that still exited 0. Deletions off."""
    reported = 100
    truncated = int(reported * _COMMENTS_SANITY_MIN_RATIO) - 1
    assert _comments_allow_deletions(
        complete=True, reported_total=reported, fetched_count=truncated
    ) is False


def test_healthy_complete_fetch_permits_deletions():
    reported = 100
    healthy = int(reported * _COMMENTS_SANITY_MIN_RATIO) + 1
    assert _comments_allow_deletions(
        complete=True, reported_total=reported, fetched_count=healthy
    ) is True
