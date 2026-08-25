"""Privacy is only versioned when a difference is really the creator's edit.

The archive reads privacy from two sources that answer different questions.
The Data API reports status.privacyStatus, which is only ever public,
unlisted or private. The desktop worker reports yt-dlp's availability, which
also knows about member-gating and maps it to "members".

Member-gating is a second axis that both sources flatten into this one
field, and the Data API cannot see it at all: a members-only video reports
privacyStatus "public". So a row the worker stored as "members" would
version to "public" on the next API rescan, claiming the creator un-gated
it, and back to "members" after the following worker refresh - one false
history entry per pass, forever, describing an edit that never happened.

That matters more than it sounds: this history is shown to the user as a
record of what happened to their own channel. A gap in it is silence. A
false entry is the archive lying about them.
"""
from __future__ import annotations

from app.metadata_rescan import (
    _api_privacy_is_comparable,
    _extract_from_api,
)


def _api_item(privacy_status):
    """A minimal videos.list item. status is omitted entirely when
    privacy_status is None, which is the shape that used to default to
    "public" and silently demote private videos."""
    item = {"snippet": {"title": "t", "description": "", "tags": []}}
    if privacy_status is not None:
        item["status"] = {"privacyStatus": privacy_status}
    return item


def test_missing_privacy_status_reads_as_unknown_not_public():
    """The bug this replaces: an absent status part became "public", which
    would demote a private video and write history saying so."""
    assert _extract_from_api(_api_item(None))["privacy"] is None
    assert _extract_from_api({"snippet": {}, "status": {}})["privacy"] is None


def test_real_tiers_are_read_through():
    for tier in ("public", "unlisted", "private"):
        assert _extract_from_api(_api_item(tier))["privacy"] == tier


def test_genuine_flips_between_api_tiers_are_still_versioned():
    """The whole point is not to stop recording real changes."""
    for old, new in (
        ("public", "private"),
        ("private", "public"),
        ("public", "unlisted"),
        ("unlisted", "private"),
    ):
        assert _api_privacy_is_comparable(old, new) is True, f"{old}->{new}"


def test_members_is_never_comparable_in_either_direction():
    """The core defect. Both directions are vocabulary, not an edit."""
    for other in ("public", "unlisted", "private"):
        assert _api_privacy_is_comparable("members", other) is False
        assert _api_privacy_is_comparable(other, "members") is False


def test_unknown_reading_is_not_a_change():
    """None means the API did not tell us. Silence is not an edit."""
    for stored in ("public", "unlisted", "private", "members", None):
        assert _api_privacy_is_comparable(stored, None) is False


def test_row_with_no_stored_privacy_is_not_treated_as_a_change():
    """Nothing to supersede, so there is no history to write. The caller
    seeds the value instead; see _apply_api_item_to_row."""
    assert _api_privacy_is_comparable(None, "public") is False
    assert _api_privacy_is_comparable("", "public") is False


def test_first_observation_seeds_without_claiming_a_change(db):
    """A first capture is not an edit.

    The first real metadata sweep in production wrote 9 description
    snapshots whose recorded old value was null or "", plus 3 viewCount
    snapshots claiming the count rose from zero. Rendered to the user that
    reads as "you wrote this description today" for text that was there all
    along. A row we have never read before has no prior observation for
    anything to have changed from.
    """
    import json as _json
    from datetime import datetime, timezone

    from app import metadata_rescan
    from app.models import User, UserChannel, UserChannelVideo, VideoFieldSnapshot

    db.add(
        User(
            id="u-seed",
            username="seed",
            email="seed@example.com",
            password_hash="$2b$12$placeholder",
        )
    )
    db.flush()
    db.add(
        UserChannel(
            user_id="u-seed", channel_id="UCseed", data_json=_json.dumps({})
        )
    )
    db.flush()
    row = UserChannelVideo(
        user_id="u-seed",
        channel_id="UCseed",
        video_id="vid1",
        # What discovery leaves behind: placeholders, never observed values.
        data_json=_json.dumps({"status": "archived", "description": "", "viewCount": 0}),
    )
    db.add(row)
    db.flush()
    assert row.last_metadata_sync_at is None, "fixture must be a first observation"

    changes = metadata_rescan._apply_api_item_to_row(
        db,
        row=row,
        api_item={
            "snippet": {
                "title": "Real title",
                "description": "A description that existed all along",
                "tags": ["a"],
                "thumbnails": {},
            },
            "status": {"privacyStatus": "public"},
            "statistics": {"viewCount": "1234"},
        },
        now=datetime.now(timezone.utc),
    )
    db.flush()

    assert changes == {}, "a first capture must not be reported as a change"
    assert db.query(VideoFieldSnapshot).count() == 0, "no history on first capture"

    # The values must still be stored - suppressing history must not also
    # throw away the data.
    stored = _json.loads(row.data_json)
    assert stored["description"] == "A description that existed all along"
    assert stored["title"] == "Real title"
    assert stored["privacy"] == "public"
    assert stored["viewCount"] == 1234


def test_second_observation_does_version_a_real_edit(db):
    """The suppression is scoped to the first look, not to everything."""
    import json as _json
    from datetime import datetime, timezone

    from app import metadata_rescan
    from app.models import User, UserChannel, UserChannelVideo, VideoFieldSnapshot

    db.add(
        User(
            id="u-second",
            username="second",
            email="second@example.com",
            password_hash="$2b$12$placeholder",
        )
    )
    db.flush()
    db.add(
        UserChannel(
            user_id="u-second", channel_id="UCsecond", data_json=_json.dumps({})
        )
    )
    db.flush()
    now = datetime.now(timezone.utc)
    row = UserChannelVideo(
        user_id="u-second",
        channel_id="UCsecond",
        video_id="vid2",
        data_json=_json.dumps({"status": "archived", "description": "old text"}),
        # Already observed once, so this is a genuine second look.
        last_metadata_sync_at=now,
    )
    db.add(row)
    db.flush()

    changes = metadata_rescan._apply_api_item_to_row(
        db,
        row=row,
        api_item={
            "snippet": {
                "title": "t",
                "description": "new text",
                "tags": [],
                "thumbnails": {},
            },
            "status": {"privacyStatus": "public"},
            "statistics": {},
        },
        now=now,
    )
    db.flush()

    assert "description" in changes
    assert changes["description"]["old"] == "old text"
    fields = [s.field for s in db.query(VideoFieldSnapshot).all()]
    assert "description" in fields
