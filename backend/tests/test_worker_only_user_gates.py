"""A worker-only user can use the product.

The platform was built around Google OAuth and later moved to a desktop
worker that runs yt-dlp locally. The permission checks were never
revisited, so several capabilities that need no credentials still demanded
authenticated ownership - and Basic tier cannot connect OAuth at all, so
those gates were not inconvenient, they were permanently shut.

The visible symptom was the Sync panel: "Discovery returned 400." and
"Metadata refresh returned 400." for the entire desktop-worker user base,
every single time, which is the whole user base.
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from app.models import User, UserChannel
from app.routes.youtube import (
    enqueue_metadata_jobs,
    sync_channel,
    sync_channel_metadata,
)


def _user(db, uid="u1"):
    u = User(
        id=uid, username=uid, email=f"{uid}@x.com",
        password_hash="p", payment_status="active",
    )
    db.add(u)
    db.flush()
    return u


def _tracked(db, user, yt="UCx"):
    """A channel added the way the product actually adds them: no OAuth."""
    row = UserChannel(
        user_id=user.id, channel_id=yt,
        google_user_id=None,
        data_json=json.dumps({"id": yt, "settings": {"active": True}}),
    )
    db.add(row)
    db.flush()
    return row


def test_discovery_does_not_fail_without_oauth(db):
    """Was: 400 for every channel the current add flow creates."""
    u = _user(db)
    _tracked(db, u)

    out = sync_channel(channel_id="UCx", db=db, current=u)

    assert out == {"discovered": 0, "updated": 0, "removed": 0}


def test_metadata_refresh_does_not_fail_without_oauth(db):
    u = _user(db)
    _tracked(db, u)

    out = sync_channel_metadata(channel_id="UCx", payload={}, db=db, current=u)

    assert out["checked"] == 0


def test_metadata_jobs_reach_a_tracked_channel(db):
    """Ownership gated this while its own comment twin gated on tracking -
    so the harder capability was open and the trivial one was not."""
    u = _user(db)
    _tracked(db, u)

    out = enqueue_metadata_jobs(db, user_id=u.id, channel_id="UCx")

    assert out["owned"] is True, "a tracked channel is reachable"


def test_an_untracked_channel_is_still_refused(db):
    """Loosening the gate must not open it to channels the user has
    nothing to do with."""
    u = _user(db)

    out = enqueue_metadata_jobs(db, user_id=u.id, channel_id="UCsomeone-else")

    assert out["owned"] is False


def test_a_removed_channel_is_still_refused(db):
    from datetime import datetime, timezone

    u = _user(db)
    row = _tracked(db, u)
    row.removed_at = datetime.now(timezone.utc)
    db.flush()

    out = enqueue_metadata_jobs(db, user_id=u.id, channel_id="UCx")

    assert out["owned"] is False


def test_unknown_channel_still_404s(db):
    u = _user(db)
    with pytest.raises(HTTPException) as e:
        sync_channel(channel_id="UCnope", db=db, current=u)
    assert e.value.status_code == 404
