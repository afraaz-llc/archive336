"""Who gets paid work, and what "pause" is allowed to mean.

Owner's rule: "a failed card should pause backups." Before this, the
nightly rescans had no payment predicate at all - they selected every
UserChannel with removed_at NULL - so an account the API already refused
kept having thumbnails and avatars re-uploaded to Backblaze every night.
Blocking channel creation never closed that, because the leak needs no
user action.

The two properties worth pinning: past_due really is paused (that was the
owner's decision, and it is the state the old code most obviously leaked
on), and pausing NEVER destroys anything - it must be reversible by the
column flipping back, or it would be a terrible response to a card that
failed for one night.
"""
from __future__ import annotations

import json

from app.models import User, UserChannel
from app.service_access import active_service_user_ids, service_is_active


def _user(db, uid, payment_status):
    u = User(
        id=uid,
        username=uid,
        email=f"{uid}@example.com",
        password_hash="$2b$12$placeholder",
        payment_status=payment_status,
    )
    db.add(u)
    db.flush()
    return u


def _track(db, user, youtube_id):
    row = UserChannel(
        user_id=user.id,
        channel_id=youtube_id,
        google_user_id=None,
        data_json=json.dumps({"id": youtube_id, "name": youtube_id}),
    )
    db.add(row)
    db.flush()
    return row


def test_only_active_is_entitled(db):
    active = _user(db, "u-active", "active")
    for uid, st in [
        ("u-none", "none"),
        ("u-pastdue", "past_due"),
        ("u-canceled", "canceled"),
    ]:
        _user(db, uid, st)

    assert service_is_active(active) is True
    assert active_service_user_ids(db) == {active.id}


def test_failed_card_pauses(db):
    """The owner's call, made explicit: past_due is paused, not carved out.

    This is the state the old cron leaked on hardest - a user whose invoice
    failed still had every channel rescanned nightly.
    """
    u = _user(db, "u-failed", "past_due")
    assert service_is_active(u) is False
    assert u.id not in active_service_user_ids(db)


def test_pause_is_reversible_and_destroys_nothing(db):
    """Pausing must be a predicate, never a deletion.

    A card can fail for one night. If pausing removed channels or archives,
    that transient failure would cost real data - so the only thing allowed
    to change is entitlement, and flipping the column back must fully
    restore service.
    """
    u = _user(db, "u-lapse", "active")
    _track(db, u, "UCkeep")

    u.payment_status = "past_due"
    db.flush()
    assert active_service_user_ids(db) == set()
    # The channel is still tracked, not removed, not soft-deleted.
    row = db.query(UserChannel).filter(UserChannel.user_id == u.id).one()
    assert row.removed_at is None, "pausing must never soft-delete a channel"

    u.payment_status = "active"
    db.flush()
    assert active_service_user_ids(db) == {u.id}, "paying again restores service"


def test_cron_selection_excludes_paused_users(db):
    """The exact query shape the three nightly jobs use."""
    payer = _user(db, "u-payer", "active")
    deadbeat = _user(db, "u-deadbeat", "past_due")
    _track(db, payer, "UCpaid")
    _track(db, deadbeat, "UCunpaid")

    entitled = active_service_user_ids(db)
    selected = (
        db.query(UserChannel)
        .filter(
            UserChannel.removed_at.is_(None),
            UserChannel.user_id.in_(entitled),
        )
        .all()
    )
    assert [r.channel_id for r in selected] == ["UCpaid"]


def test_api_gate_and_crons_share_one_predicate(db):
    """They must not drift. security.get_paid_user is the HTTP surface and
    the crons are the batch surface; a user either gets paid work from both
    or from neither."""
    from app import security

    for st in ("none", "past_due", "canceled", "active"):
        u = _user(db, f"u-drift-{st}", st)
        http_allows = security.service_is_active(u)
        cron_allows = u.id in active_service_user_ids(db)
        assert http_allows == cron_allows, f"surfaces disagree for {st}"
