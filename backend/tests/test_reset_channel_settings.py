"""Re-applying account defaults to a channel you already have.

Defaults were applied exactly once, at add time, and never again - so
changing them left every existing channel wearing whatever it was created
with, and the only remedy in the product was to remove the channel and add
it back. That is what this replaces.
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from app import archive
from app.models import User, UserChannel, UserYouTubeSettings
from app.routes.youtube import reset_channel_settings


def _user(db):
    u = User(id="u1", username="u1", email="u1@x.com", password_hash="p")
    db.add(u)
    db.flush()
    return u


def _channel(db, user, settings, *, removed_at=None):
    row = UserChannel(
        user_id=user.id, channel_id="UCx", google_user_id=None,
        data_json=json.dumps({"id": "UCx", "name": "X", "settings": settings}),
        removed_at=removed_at,
    )
    db.add(row)
    db.flush()
    return row


def test_channel_takes_the_users_current_defaults(db):
    u = _user(db)
    db.add(UserYouTubeSettings(
        user_id=u.id,
        settings_json=json.dumps({"maxResolution": "source", "saveTags": True}),
    ))
    row = _channel(db, u, {"maxResolution": "720p", "saveTags": False, "active": True})
    db.flush()

    out = reset_channel_settings(channel_id="UCx", db=db, current=u)

    assert out["settings"]["maxResolution"] == "source"
    assert out["settings"]["saveTags"] is True
    stored = json.loads(db.get(UserChannel, (u.id, "UCx")).data_json)["settings"]
    assert stored["maxResolution"] == "source"


def test_active_is_preserved(db):
    """Resetting preferences must never quietly pause a channel - that
    would stop backups without saying so."""
    u = _user(db)
    db.add(UserYouTubeSettings(
        user_id=u.id, settings_json=json.dumps({"active": False})
    ))
    _channel(db, u, {"active": True, "maxResolution": "720p"})
    db.flush()

    out = reset_channel_settings(channel_id="UCx", db=db, current=u)

    assert out["settings"]["active"] is True, "a running channel stays running"


def test_a_paused_channel_stays_paused(db):
    u = _user(db)
    db.add(UserYouTubeSettings(
        user_id=u.id, settings_json=json.dumps({"active": True})
    ))
    _channel(db, u, {"active": False})
    db.flush()

    out = reset_channel_settings(channel_id="UCx", db=db, current=u)

    assert out["settings"]["active"] is False


def _pooled(db, user, settings):
    """Give the user the shared-pool rows the read path actually serves."""
    ch = archive.ensure_channel(db, "UCx", title="X")
    sub = archive.ensure_subscription(db, user.id, ch.id)
    sub.settings_json = json.dumps(settings)
    db.flush()
    return ch, sub


def test_reset_reaches_the_row_the_frontend_reads(db):
    """The regression. Per-channel settings live in two rows - legacy
    UserChannel.data_json and shared-pool Subscription.settings_json -
    and every read serves the second one. Reset wrote only the first, so
    it stored correctly, returned the right body, and changed nothing the
    user could see: the panel kept showing 720p while the legacy table
    said "source". Asserting on the write target (as every other test in
    this file does) passes either way, so this one asserts through
    channel_response_payload - the same call the GET route makes.
    """
    u = _user(db)
    db.add(UserYouTubeSettings(
        user_id=u.id,
        settings_json=json.dumps({"maxResolution": "source", "saveTags": True}),
    ))
    stale = {"maxResolution": "720p", "saveTags": False, "active": True}
    _channel(db, u, stale)
    ch, sub = _pooled(db, u, stale)

    reset_channel_settings(channel_id="UCx", db=db, current=u)
    db.flush()

    served = archive.channel_response_payload(ch, sub)["settings"]
    assert served["maxResolution"] == "source"
    assert served["saveTags"] is True


def test_reset_diffs_against_the_settings_actually_in_force(db):
    """`active` is preserved from what the channel is really running,
    not from a legacy mirror that has drifted. A user who paused a
    channel must not have it silently resumed because the stale copy
    still said active."""
    u = _user(db)
    db.add(UserYouTubeSettings(
        user_id=u.id, settings_json=json.dumps({"active": True})
    ))
    _channel(db, u, {"active": True})          # drifted mirror
    _pooled(db, u, {"active": False})          # what the user actually set

    out = reset_channel_settings(channel_id="UCx", db=db, current=u)

    assert out["settings"]["active"] is False


def test_unknown_channel_404s(db):
    u = _user(db)
    with pytest.raises(HTTPException) as e:
        reset_channel_settings(channel_id="UCnope", db=db, current=u)
    assert e.value.status_code == 404


def test_a_removed_channel_cannot_be_reset(db):
    from datetime import datetime, timezone

    u = _user(db)
    _channel(db, u, {"active": True}, removed_at=datetime.now(timezone.utc))
    db.flush()

    with pytest.raises(HTTPException) as e:
        reset_channel_settings(channel_id="UCx", db=db, current=u)
    assert e.value.status_code == 404
