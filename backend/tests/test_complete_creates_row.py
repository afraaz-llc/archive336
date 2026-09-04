"""A successful sync must record the archive even with no prior row.

Everything in complete_sync_job that records a finished download was
gated on `if video:` - the per-user UserChannelVideo row. A video whose
first SUCCESS arrives before any row exists therefore recorded nothing:
no archived status, and no record_synced_video, which is what puts
r2_key on the shared pool row.

The queue reads "already archived" from exactly the status that never
got written, so it re-enqueued the video on the next pass. On the
owner's own deployment one video downloaded and uploaded successfully
34 times over three days, every 30 minutes, each success discarded -
and every upload wrote another version of the same object, which is the
failure mode that filled the bucket and took storage down once already.
"""
from __future__ import annotations

import json

from app import archive
from app.models import SyncJob, User, UserChannelVideo, Video


def _user(db, uid="u1"):
    u = User(
        id=uid,
        username=uid,
        email=f"{uid}@example.com",
        password_hash="$2b$12$placeholder",
    )
    db.add(u)
    db.flush()
    return u


def test_a_success_with_no_row_still_marks_the_video_archived(db, monkeypatch):
    from app.routes import youtube as yt

    u = _user(db)
    ch = archive.ensure_channel(db, "UCaaa", title="Alpha")
    archive.ensure_subscription(db, u.id, ch.id)
    archive.ensure_placeholder_video(
        db, channel=ch, youtube_video_id="v1", title="never recorded",
        privacy="public",
    )
    job = SyncJob(
        user_id=u.id, channel_id="UCaaa", video_id="v1", kind="video",
        status="running", claimed_by=u.id,
    )
    db.add(job)
    db.flush()

    assert db.query(UserChannelVideo).count() == 0, "the case that broke"

    # The upload is real as far as the route is concerned.
    monkeypatch.setattr(
        yt.r2, "head",
        lambda *a, **k: {"ContentLength": 2048, "ContentType": "video/mp4"},
    )
    monkeypatch.setattr(yt.r2, "metadata_bytes_for", lambda **k: 0)
    monkeypatch.setattr(
        yt.storage_ledger, "record_object", lambda *a, **k: None
    )

    yt.complete_sync_job(
        job_id=job.id,
        payload={"availability": "public"},
        db=db,
        current=u,
    )

    row = db.query(UserChannelVideo).filter_by(
        user_id=u.id, video_id="v1"
    ).one_or_none()
    assert row is not None, "the row is created rather than the work discarded"
    assert (json.loads(row.data_json) or {}).get("status") == "archived"

    pooled = db.query(Video).filter(Video.youtube_id == "v1").one()
    assert pooled.r2_key, "pool row carries the file, so nothing re-queues it"
