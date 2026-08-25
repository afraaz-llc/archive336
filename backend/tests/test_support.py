"""Support conversations, and the snapshot that makes them answerable.

The reason this is built rather than bought: every question a user asks
about a backup tool is a question about state they cannot see. "Why
isn't it syncing" has at least six answers and the user can distinguish
none of them. The snapshot is taken server-side at send time so the
maintainer can answer without a round trip.
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from app import archive
from app.models import (
    SupportMessage,
    SyncJob,
    User,
    UserChannelSubscription,
    WorkerYoutubeConnection,
)
from app.routes.support import admin_reply, my_thread, post_message
from app import support as support_lib


def _user(db, uid="u1", admin=False):
    u = User(
        id=uid, username=uid, email=f"{uid}@x.com", password_hash="p",
        payment_status="active",
    )
    db.add(u)
    db.flush()
    return u


def test_a_message_is_stored_with_a_snapshot(db):
    u = _user(db)
    ch = archive.ensure_channel(db, "UCx", title="X")
    sub = UserChannelSubscription(user_id=u.id, channel_id=ch.id)
    sub.settings_json = json.dumps({"active": True})
    db.add(sub)
    db.add(SyncJob(
        user_id=u.id, channel_id="UCx", video_id="v1",
        kind="video", status="pending",
    ))
    db.add(WorkerYoutubeConnection(user_id=u.id, connected=True, cookie_count=40))
    db.flush()

    post_message({"body": "my backup stopped", "kind": "bug"}, db=db, current=u)

    stored = db.query(SupportMessage).one()
    assert stored.from_staff is False
    snap = json.loads(stored.snapshot_json)
    assert snap["jobs"]["pending"] == 1
    assert snap["worker"]["connected"] is True
    assert snap["channels"][0]["active"] is True


def test_the_snapshot_never_goes_back_to_the_user(db):
    """It is the maintainer's evidence. Handing a user their own
    diagnostic dump invites them to debug it, which is the job this
    feature exists to take off them."""
    u = _user(db)
    post_message({"body": "hello", "kind": "question"}, db=db, current=u)

    out = my_thread(db=db, current=u)

    assert len(out["messages"]) == 1
    assert "snapshot" not in out["messages"][0]


def test_an_empty_message_is_refused(db):
    u = _user(db)
    with pytest.raises(HTTPException) as e:
        post_message({"body": "   "}, db=db, current=u)
    assert e.value.status_code == 400


def test_an_unknown_kind_falls_back_rather_than_failing(db):
    """A bad `kind` is our problem, not something to lose a user's
    message over."""
    u = _user(db)
    post_message({"body": "hi", "kind": "nonsense"}, db=db, current=u)
    assert db.query(SupportMessage).one().kind == "question"


def test_a_reply_joins_the_same_thread(db):
    u = _user(db)
    admin = _user(db, "admin")
    post_message({"body": "help"}, db=db, current=u)

    admin_reply(u.id, {"body": "looking now"}, db=db, _admin=admin)

    msgs = my_thread(db=db, current=u)["messages"]
    assert [m["fromStaff"] for m in msgs] == [False, True]


def test_a_thread_is_private_to_its_user(db):
    """One conversation per user, and nobody else's."""
    a = _user(db, "alice")
    b = _user(db, "bob")
    post_message({"body": "alice here"}, db=db, current=a)

    assert my_thread(db=db, current=b)["messages"] == []


def test_snapshot_lines_reads_as_prose(db):
    u = _user(db)
    db.add(WorkerYoutubeConnection(user_id=u.id, connected=False, cookie_count=0))
    db.flush()
    text = support_lib.snapshot_lines(support_lib.account_snapshot(db, u))
    assert "not connected" in text
    assert "u1@x.com" in text


def test_one_error_repeating_does_not_fill_the_snapshot(db):
    """The first real alert this produced listed "This video is private"
    four times and nothing else. One noisy video must not hide every
    other failure - the maintainer needs the range of what is wrong."""
    from datetime import datetime, timezone
    from app.models import SyncJob

    u = _user(db)
    for i in range(8):
        db.add(SyncJob(
            user_id=u.id, channel_id="UCx", video_id=f"noisy{i}",
            kind="video", status="failed",
            error="yt-dlp failed: ERROR: [youtube] abc: Video unavailable.",
            created_at=datetime.now(timezone.utc),
        ))
    db.add(SyncJob(
        user_id=u.id, channel_id="UCx", video_id="other",
        kind="video", status="failed", error="r2 put http 403 Forbidden",
        created_at=datetime.now(timezone.utc),
    ))
    db.flush()

    errs = support_lib.account_snapshot(db, u)["recentErrors"]

    assert len(errs) == 2, errs
    assert any("x8" in e for e in errs), "repeats are counted, not repeated"
    assert any("403" in e for e in errs), "the other failure still shows"
