"""Removal detection on the no-OAuth (channel enumeration) path.

These tests exist because of one specific failure mode. Basic-tier users
authenticate through the desktop worker and have no web OAuth, so their
upkeep runs off a channel-tab enumeration. That listing can only ever
show a channel's PUBLIC videos - a private, unlisted or members-only
video is invisible to it by definition, forever, no matter how healthy
the archive is.

If absence on that listing were read as evidence of removal, every
private video in an archive would take a strike on every run and the
user would be emailed that their private videos had been deleted. On the
production account that is 6 of 7 archived videos.

So the rule under test is: absence only counts for videos the listing
could actually have shown. Everything here is pinned with real numbers
so a regression fails loudly rather than quietly mailing a lie.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app import metadata_rescan
from app.models import User, UserChannel, UserChannelVideo


USER_ID = "u-owner"
CHANNEL_ID = "UCtest"


@pytest.fixture(autouse=True)
def _owner(db):
    """UserChannelVideo hangs off a real user and a real tracked channel,
    both enforced by foreign keys. Every test here needs the pair."""
    db.add(
        User(
            id=USER_ID,
            username="owner",
            email="owner@example.com",
            password_hash="$2b$12$placeholder",
        )
    )
    db.flush()
    db.add(
        UserChannel(
            user_id=USER_ID,
            channel_id=CHANNEL_ID,
            data_json=json.dumps({"settings": {}}),
        )
    )
    db.flush()


def _video(db, video_id, *, privacy, status="archived", seen_on_tab=None):
    data = {
        "id": video_id,
        "status": status,
        "privacy": privacy,
        "localPath": f"/tmp/{video_id}.mp4",
    }
    if seen_on_tab is not None:
        data["lastSeenOnChannelTabAt"] = seen_on_tab
    row = UserChannelVideo(
        user_id=USER_ID,
        channel_id=CHANNEL_ID,
        video_id=video_id,
        data_json=json.dumps(data),
    )
    db.add(row)
    db.flush()
    return row


def _data(row):
    """Read the row's blob back. Note the tests flush rather than refresh:
    reconcile mutates data_json in memory and leaves committing to the
    caller, so db.refresh() would expire the instance and reload the
    pre-change row from SQLite, quietly discarding the very writes under
    test."""
    return json.loads(row.data_json)


def _strikes(row):
    return _data(row).get("removalMissCount") or 0


def test_private_videos_never_take_an_absence_strike(db):
    """The load-bearing test. A healthy archive of private videos plus one
    public video, enumerated normally: the private rows must come through
    completely untouched, however many times this runs."""
    private_rows = [
        _video(db, f"priv{i}", privacy=p)
        for i, p in enumerate(
            ["private", "private", "unlisted", "members", "members_only", "private"]
        )
    ]
    public_row = _video(db, "pub1", privacy="public", seen_on_tab="2026-01-01T00:00:00+00:00")

    rows = private_rows + [public_row]
    now = datetime.now(timezone.utc)

    # Two runs, spaced far enough apart that the debounce would allow a
    # second strike to land if one were ever banked.
    for offset in (0, 1):
        metadata_rescan.reconcile_against_enumeration(
            db,
            rows=rows,
            present_video_ids={"pub1"},
            now=now + timedelta(days=offset),
        )

    for row in private_rows:
        db.flush()
        assert _strikes(row) == 0, f"{row.video_id} took a strike"
        assert _data(row)["status"] == "archived"
        assert _data(row).get("deletedOnYoutubeAt") is None


def test_enumeration_candidate_filter_excludes_sealed_rows(db):
    """The filter callers use before spending a request. Public rows are
    candidates; every sealed tier is not."""
    assert metadata_rescan.enumeration_can_see_row(
        _video(db, "pub", privacy="public")
    ) is True
    for privacy in ("private", "unlisted", "members", "members_only"):
        row = _video(db, f"x-{privacy}", privacy=privacy)
        assert metadata_rescan.enumeration_can_see_row(row) is False, privacy


def test_unparseable_row_is_not_a_candidate(db):
    """We will not reason about the visibility of a row we cannot read."""
    row = UserChannelVideo(
        user_id=USER_ID,
        channel_id=CHANNEL_ID,
        video_id="broken",
        data_json="{not json",
    )
    db.add(row)
    db.flush()
    assert metadata_rescan.enumeration_can_see_row(row) is False


def test_public_video_needs_two_strikes_and_only_then_reports(db):
    """A genuinely delisted public video is detected, but never on the
    first miss, and the removal is reported exactly once."""
    row = _video(db, "pub1", privacy="public", seen_on_tab="2026-01-01T00:00:00+00:00")
    now = datetime.now(timezone.utc)

    sink = {}
    metadata_rescan.reconcile_against_enumeration(
        db, rows=[row], present_video_ids=set(), now=now, removal_sink=sink
    )
    db.flush()
    assert _strikes(row) == 1
    assert _data(row)["status"] == "archived"
    assert sink == {}, "a single miss must not notify"

    metadata_rescan.reconcile_against_enumeration(
        db,
        rows=[row],
        present_video_ids=set(),
        now=now + timedelta(days=1),
        removal_sink=sink,
    )
    db.flush()
    assert _data(row)["status"] == "deleted_on_youtube"
    assert _data(row)["deletedOnYoutubeAt"] is not None
    # The sink carries the video ids that were confirmed removed, not a bare
    # count, so a single removal can name its video in the email.
    assert sink == {(USER_ID, CHANNEL_ID): ["pub1"]}

    # A third miss must not re-report the same transition.
    sink.clear()
    metadata_rescan.reconcile_against_enumeration(
        db,
        rows=[row],
        present_video_ids=set(),
        now=now + timedelta(days=2),
        removal_sink=sink,
    )
    assert sink == {}, "an already-confirmed removal must not re-notify"


def test_two_misses_inside_the_debounce_window_count_once(db):
    """Rapid re-runs are one observation, not two. Otherwise a ten-second
    YouTube blip seen twice would 'confirm' a removal."""
    row = _video(db, "pub1", privacy="public", seen_on_tab="2026-01-01T00:00:00+00:00")
    now = datetime.now(timezone.utc)

    for offset in (0, 60):
        metadata_rescan.reconcile_against_enumeration(
            db,
            rows=[row],
            present_video_ids=set(),
            now=now + timedelta(seconds=offset),
        )
    db.flush()
    assert _strikes(row) == 1
    assert _data(row)["status"] == "archived"


def test_reappearing_video_clears_its_banked_strike(db):
    """A video seen again is healthy again, from strike zero."""
    row = _video(db, "pub1", privacy="public", seen_on_tab="2026-01-01T00:00:00+00:00")
    now = datetime.now(timezone.utc)

    metadata_rescan.reconcile_against_enumeration(
        db, rows=[row], present_video_ids=set(), now=now
    )
    db.flush()
    assert _strikes(row) == 1

    metadata_rescan.reconcile_against_enumeration(
        db,
        rows=[row],
        present_video_ids={"pub1"},
        now=now + timedelta(days=1),
    )
    db.flush()
    assert _strikes(row) == 0
    assert _data(row).get("deletedOnYoutubeAt") is None


def test_never_listed_public_row_is_checked_but_never_struck(db):
    """A public Short is absent from the /videos tab forever. Being absent
    forever is not evidence of anything, so a row we have never once seen
    on the tab must not accumulate strikes toward deletion."""
    row = _video(db, "short1", privacy="public")  # never seen on the tab
    now = datetime.now(timezone.utc)

    for offset in (0, 1, 2):
        metadata_rescan.reconcile_against_enumeration(
            db,
            rows=[row],
            present_video_ids={"somethingelse"},
            now=now + timedelta(days=offset),
        )
    db.flush()
    assert _data(row)["status"] == "archived"
    assert _data(row).get("deletedOnYoutubeAt") is None
