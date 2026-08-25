"""The New-channel-defaults panel must actually govern new channels.

Before _new_channel_settings existed, every add path honored exactly one of
the panel's keys ("active") and hardcoded the rest - a settings panel whose
settings mostly did nothing. The contract under test:

  - a fresh channel wears the user's saved defaults over the app baseline,
  - removal wipes per-channel settings, so a re-add inside the 30-day
    grace window comes back wearing the CURRENT defaults too,
  - unknown keys in the saved blob never reach a channel,
  - a user who never configured defaults gets the app baseline unchanged.
"""
from __future__ import annotations

import json

from app.models import User, UserYouTubeSettings
from app.routes.youtube import (
    _DEFAULT_CHANNEL_SETTINGS,
    _new_channel_settings,
)


def _user(db, uid="u-defaults"):
    u = User(
        id=uid,
        username=uid,
        email=f"{uid}@example.com",
        password_hash="$2b$12$placeholder",
    )
    db.add(u)
    db.flush()
    return u


def test_no_saved_defaults_yields_the_app_baseline(db):
    u = _user(db)
    assert _new_channel_settings(db, u.id) == _DEFAULT_CHANNEL_SETTINGS


def test_saved_defaults_overlay_the_baseline(db):
    u = _user(db)
    db.add(
        UserYouTubeSettings(
            user_id=u.id,
            settings_json=json.dumps(
                {
                    "active": False,
                    "saveDescription": False,
                    "maxResolution": "1080p",
                    "saveDescriptionHistory": True,
                }
            ),
        )
    )
    db.flush()
    s = _new_channel_settings(db, u.id)
    # The user's choices win...
    assert s["active"] is False
    assert s["saveDescription"] is False
    assert s["maxResolution"] == "1080p"
    assert s["saveDescriptionHistory"] is True
    # ...and everything they did not touch stays baseline.
    assert s["saveTags"] == _DEFAULT_CHANNEL_SETTINGS["saveTags"]
    assert s["filterPresets"] == _DEFAULT_CHANNEL_SETTINGS["filterPresets"]


def test_unknown_keys_are_not_seeded_onto_channels(db):
    u = _user(db)
    db.add(
        UserYouTubeSettings(
            user_id=u.id,
            settings_json=json.dumps(
                {"active": True, "totallyMadeUpKey": "boo", "__proto__": 1}
            ),
        )
    )
    db.flush()
    s = _new_channel_settings(db, u.id)
    assert "totallyMadeUpKey" not in s
    assert "__proto__" not in s


def test_corrupt_saved_blob_falls_back_to_baseline(db):
    u = _user(db)
    db.add(UserYouTubeSettings(user_id=u.id, settings_json="{not json"))
    db.flush()
    assert _new_channel_settings(db, u.id) == _DEFAULT_CHANNEL_SETTINGS


def test_result_is_a_fresh_copy_not_a_shared_reference(db):
    """filterPresets is a nested list; two channels must never share one."""
    u = _user(db)
    a = _new_channel_settings(db, u.id)
    a["filterPresets"].append({"id": "mutated"})
    b = _new_channel_settings(db, u.id)
    assert all(p.get("id") != "mutated" for p in b["filterPresets"])
    assert all(
        p.get("id") != "mutated" for p in _DEFAULT_CHANNEL_SETTINGS["filterPresets"]
    )


def test_baseline_carries_no_retired_cadence():
    """"manual" was retired; the frontend coerces it away on read, so
    seeding it would hand every new channel an invalid value."""
    assert _DEFAULT_CHANNEL_SETTINGS["commentsRefreshFrequency"] != "manual"
    assert _DEFAULT_CHANNEL_SETTINGS["metadataRefreshFrequency"] != "manual"
