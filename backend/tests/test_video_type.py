"""Short / livestream / video, as the Type filter promises.

Nothing had been setting this. The classifier that existed read a
YouTube Data API snippet, and discovery moved to the worker long ago, so
every video in every archive was typed "video" - the Type filter offered
Short and Livestream and returned an empty list for both. The owner
found it by filtering for Shorts on a channel that has them.
"""
from __future__ import annotations

from app.archive import classify_video_type


def test_a_vertical_clip_is_a_short():
    assert classify_video_type(
        {"durationSec": 12, "videoResolution": "590x1280"}
    ) == "short"


def test_square_counts_as_short_shape():
    """YouTube's rule is square or taller, not strictly taller."""
    assert classify_video_type(
        {"durationSec": 30, "videoResolution": "1080x1080"}
    ) == "short"


def test_a_short_landscape_clip_is_not_a_short():
    """Length alone never makes a Short - a 10-second landscape clip is
    a video, and typing it as a Short would put it in a shelf YouTube
    never put it in."""
    assert classify_video_type(
        {"durationSec": 10, "videoResolution": "1920x1080"}
    ) == "video"


def test_a_long_vertical_video_is_not_a_short():
    """Three minutes is the cutoff YouTube uses."""
    assert classify_video_type(
        {"durationSec": 200, "videoResolution": "1080x1920"}
    ) == "video"
    assert classify_video_type(
        {"durationSec": 180, "videoResolution": "1080x1920"}
    ) == "short"


def test_no_resolution_reading_means_no_claim():
    """A video we have never downloaded has no shape on file. Guessing
    from length alone would type every trailer as a Short."""
    assert classify_video_type({"durationSec": 30}) == "video"
    assert classify_video_type(
        {"durationSec": 30, "videoResolution": "unknown"}
    ) == "video"


def test_a_zero_duration_is_not_a_reading():
    """0 is what an unsynced row carries, not a zero-length video."""
    assert classify_video_type(
        {"durationSec": 0, "videoResolution": "1080x1920"}
    ) == "video"


def test_was_live_wins_over_shape():
    """A vertical clip of a stream is still a stream."""
    assert classify_video_type(
        {"wasLive": True, "durationSec": 20, "videoResolution": "1080x1920"}
    ) == "livestream"


def test_was_live_only_counts_when_it_is_true():
    """Absent or false is "we have no reading", not "not a stream"."""
    assert classify_video_type(
        {"wasLive": None, "durationSec": 12, "videoResolution": "590x1280"}
    ) == "short"


def test_junk_survives_classification():
    for data in ({}, {"durationSec": "12"}, {"durationSec": True},
                 {"videoResolution": "0x0", "durationSec": 5},
                 {"videoResolution": "x", "durationSec": 5}):
        assert classify_video_type(data) == "video"
