"""Validation of the worker's metadata payload.

The rule the whole feature hangs off: a payload short of title, description
or tags is "we could not look" and is rejected whole, because the engine
cannot tell a missing field from a cleared one and would faithfully record
that the creator deleted their description.

Privacy is the exception, and this is the case that motivated it. An
age-restricted video reports yt-dlp availability "needs_auth" even though
its title, description and tags all extract perfectly. Sinking that whole
good read over an unconfirmable privacy label left every age-gated video
with no upkeep at all. Both production failures on the first real sweep
were exactly this. So privacy is optional: a confirmed reading is carried
through, an absent or unreadable one is dropped and the stored value is
left alone.
"""
from __future__ import annotations

from app.routes.youtube import _worker_metadata_fields


def _meta(**over):
    base = {
        "title": "A title",
        "description": "some description",
        "tags": ["a", "b"],
        "privacy": "public",
    }
    base.update(over)
    return base


def test_full_payload_is_accepted():
    fields = _worker_metadata_fields(_meta())
    assert fields is not None
    assert fields["title"] == "A title"
    assert fields["privacy"] == "public"


def test_age_restricted_needs_auth_keeps_the_read_and_drops_privacy():
    """The core regression. needs_auth is not a privacy tier; the metadata
    is still good and must be applied without a privacy claim."""
    fields = _worker_metadata_fields(_meta(privacy="needs_auth"))
    assert fields is not None, "a needs_auth read must not be rejected whole"
    assert fields["title"] == "A title"
    assert fields["description"] == "some description"
    assert fields["tags"] == ["a", "b"]
    assert "privacy" not in fields, "an unconfirmable privacy is not carried"


def test_privacy_absent_entirely_is_fine():
    m = _meta()
    del m["privacy"]
    fields = _worker_metadata_fields(m)
    assert fields is not None
    assert "privacy" not in fields


def test_real_privacy_tiers_are_carried_through():
    for tier in ("public", "unlisted", "private"):
        assert _worker_metadata_fields(_meta(privacy=tier))["privacy"] == tier


def test_missing_title_is_still_rejected_whole():
    m = _meta()
    del m["title"]
    assert _worker_metadata_fields(m) is None
    assert _worker_metadata_fields(_meta(title="")) is None
    assert _worker_metadata_fields(_meta(title="   ")) is None


def test_missing_description_or_tags_is_rejected_whole():
    m = _meta()
    del m["description"]
    assert _worker_metadata_fields(m) is None
    m2 = _meta()
    del m2["tags"]
    assert _worker_metadata_fields(m2) is None
    assert _worker_metadata_fields(_meta(tags="notalist")) is None
    assert _worker_metadata_fields(_meta(tags=[1, 2])) is None


def test_empty_description_is_a_real_value():
    """A video genuinely can have no description; empty string is not a
    failed read the way an empty title is."""
    fields = _worker_metadata_fields(_meta(description=""))
    assert fields is not None
    assert fields["description"] == ""


def test_optional_stats_are_carried_only_when_well_formed():
    fields = _worker_metadata_fields(_meta(viewCount=52, durationSec=3933))
    assert fields["viewCount"] == 52
    assert fields["durationSec"] == 3933
    # bool must not sneak through as a count of 1.
    assert "viewCount" not in _worker_metadata_fields(_meta(viewCount=True))
    # negative / zero duration is not a real value.
    assert "durationSec" not in _worker_metadata_fields(_meta(durationSec=0))
