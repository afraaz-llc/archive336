"""Noticing that somebody's backup has stopped.

Written from a real outage: the storage bucket filled, every upload
started 403ing, and the queue stopped dead. Nothing in the product said
so - the worker reported "running", the website showed active channels -
and it surfaced only because the owner happened to ask why a video had
not appeared.

The rule these pin is "work exists and is not moving", not "an error
happened". Errors are ordinary; a queue that has not advanced while a
worker is alive to advance it is not.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app import queue_health
from app.models import SyncJob, User, WorkerYoutubeConnection

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def _user(db, uid="u1"):
    u = User(
        id=uid, username=uid, email=f"{uid}@x.com",
        password_hash="p", payment_status="active",
    )
    db.add(u)
    db.flush()
    return u


def _worker(db, uid="u1", *, connected=True, ago=timedelta(minutes=5)):
    db.add(WorkerYoutubeConnection(
        user_id=uid, connected=connected, cookie_count=40,
        reported_at=NOW - ago,
    ))
    db.flush()


def _job(db, *, uid="u1", status, finished_ago=None, error=None, created_ago=timedelta(hours=1)):
    db.add(SyncJob(
        user_id=uid, channel_id="UCx", video_id=f"v{status}{finished_ago}{error}",
        kind="video", status=status, error=error,
        created_at=NOW - created_ago,
        finished_at=None if finished_ago is None else NOW - finished_ago,
    ))
    db.flush()


def test_a_stalled_queue_is_reported(db):
    """The production case: queued work, a live worker, nothing landing."""
    _user(db)
    _worker(db)
    _job(db, status="pending")
    _job(db, status="done", finished_ago=timedelta(hours=20))

    stalled = queue_health.find_stalled_users(db, now=NOW)

    assert len(stalled) == 1
    assert stalled[0].pending == 1


def test_a_moving_queue_is_not_reported(db):
    """Something finished recently, so the queue is working through it.
    A big video in flight must never read as an outage."""
    _user(db)
    _worker(db)
    _job(db, status="pending")
    _job(db, status="done", finished_ago=timedelta(hours=1))

    assert queue_health.find_stalled_users(db, now=NOW) == []


def test_an_offline_worker_is_not_an_operator_problem(db):
    """A closed laptop is the customer's own doing and the most common
    state in the product. Paging on it would bury the real signal."""
    _user(db)
    _worker(db, ago=timedelta(days=2))
    _job(db, status="pending")

    assert queue_health.find_stalled_users(db, now=NOW) == []


def test_no_queued_work_is_not_a_stall(db):
    """Nothing to do is the healthy resting state, not a fault."""
    _user(db)
    _worker(db)
    _job(db, status="done", finished_ago=timedelta(days=5))

    assert queue_health.find_stalled_users(db, now=NOW) == []


def test_a_queue_that_never_completed_anything_counts(db):
    """A brand-new user whose first sync never moves has no last
    completion at all - the case an "oldest completion" check would
    silently skip."""
    _user(db)
    _worker(db)
    _job(db, status="pending")

    assert len(queue_health.find_stalled_users(db, now=NOW)) == 1


def test_repeated_identical_failures_are_a_storm(db):
    _user(db)
    for _ in range(queue_health.FAILURE_STORM_COUNT):
        _job(
            db, status="failed", created_ago=timedelta(minutes=10),
            error="r2 put http 403 Forbidden: storage cap exceeded.",
        )

    storms = queue_health.find_failure_storms(db, now=NOW)

    assert len(storms) == 1
    assert storms[0].count == queue_health.FAILURE_STORM_COUNT


def test_a_few_failures_are_not_a_storm(db):
    """Videos get deleted and laptops close mid-download. Alerting on
    ordinary failure is how an operator learns to ignore alerts."""
    _user(db)
    for _ in range(3):
        _job(db, status="failed", created_ago=timedelta(minutes=10), error="boom")

    assert queue_health.find_failure_storms(db, now=NOW) == []


def test_cancelled_work_is_never_a_storm(db):
    """Removing a channel marks its queue failed with a cancelled:
    prefix. That is the user's decision, not an incident."""
    _user(db)
    for _ in range(20):
        _job(
            db, status="failed", created_ago=timedelta(minutes=5),
            error="cancelled: channel removed",
        )

    assert queue_health.find_failure_storms(db, now=NOW) == []


def test_a_storm_carries_the_queue_it_came_from(db):
    """The alert named the wrong queue because storms were just an error
    string and a count. A night where every failure was the comment
    rescan went out as "A backup queue has stalled" while backups were
    entirely healthy."""
    u = _user(db, "u1")
    for i in range(queue_health.FAILURE_STORM_COUNT):
        db.add(SyncJob(
            user_id=u.id, channel_id="UCx", video_id=f"v{i}",
            kind="comments", status="failed",
            error="comment read signed out, wrote nothing: the YouTube session was stale",
        ))
    db.flush()

    storms = queue_health.find_failure_storms(db)

    assert len(storms) == 1
    assert storms[0].kind == "comments", "not reported as a backup failure"


def test_the_same_error_from_two_queues_is_two_storms(db):
    """Grouping on the message alone would merge them and pick whichever
    kind happened to be read first."""
    u = _user(db, "u1")
    for kind in ("video", "comments"):
        for i in range(queue_health.FAILURE_STORM_COUNT):
            db.add(SyncJob(
                user_id=u.id, channel_id="UCx", video_id=f"{kind}{i}",
                kind=kind, status="failed", error="timed out",
            ))
    db.flush()

    kinds = {s.kind for s in queue_health.find_failure_storms(db)}
    assert kinds == {"video", "comments"}
