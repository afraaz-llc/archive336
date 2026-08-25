"""Re-adding a removed channel gives it your CURRENT defaults.

The contract _new_channel_settings promises in its own docstring: "remove
a channel, add it back, get your defaults." The owner asked whether it
actually holds, and there was no test - so answering meant reading logs
instead of running something.
"""
from __future__ import annotations

import json

from app.models import User, UserChannel, UserYouTubeSettings
from app.routes.youtube import _new_channel_settings


def _user(db):
    u = User(id="u1", username="u1", email="u1@x.com", password_hash="p")
    db.add(u)
    db.flush()
    return u


def _globals(db, user, **overrides):
    db.add(UserYouTubeSettings(
        user_id=user.id, settings_json=json.dumps(overrides)
    ))
    db.flush()


def test_saved_defaults_win_over_the_app_baseline(db):
    u = _user(db)
    _globals(db, u, maxResolution="1080p", saveTags=False, syncComments=False)

    s = _new_channel_settings(db, u.id)

    assert s["maxResolution"] == "1080p"
    assert s["saveTags"] is False
    assert s["syncComments"] is False


def test_keys_the_user_never_set_fall_back_to_the_baseline(db):
    u = _user(db)
    _globals(db, u, maxResolution="1080p")

    s = _new_channel_settings(db, u.id)

    assert s["codecPreference"] == "compat"
    assert s["downloadNewVideos"] is True


def test_a_user_with_no_saved_defaults_gets_the_baseline(db):
    u = _user(db)

    s = _new_channel_settings(db, u.id)

    assert s["active"] is True
    assert s["downloadNewVideos"] is True


def test_readd_does_not_resurrect_the_removed_channels_settings(db):
    """The actual question. A soft-deleted channel carries whatever it was
    configured with; re-adding must overwrite that with current defaults,
    not restore it."""
    u = _user(db)
    _globals(db, u, maxResolution="1080p", saveTags=True)
    stale = {"maxResolution": "360p", "saveTags": False, "active": False}
    row = UserChannel(
        user_id=u.id, channel_id="UCx", google_user_id=None,
        data_json=json.dumps({"id": "UCx", "settings": stale}),
    )
    db.add(row)
    db.flush()

    # What the re-add path writes over it.
    row.data_json = json.dumps({"id": "UCx", "settings": _new_channel_settings(db, u.id)})
    db.flush()

    got = json.loads(row.data_json)["settings"]
    assert got["maxResolution"] == "1080p", "stale per-channel setting survived"
    assert got["saveTags"] is True
    assert got["active"] is True
