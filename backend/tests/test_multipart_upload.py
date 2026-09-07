"""Multipart upload, for videos too big for a single presigned PUT.

A presigned PutObject is one request and S3 caps that at 5 GB. Videos
upload that way, so a 10-hour video downloaded fine - 7.1 GB, 22
minutes - and was then refused with EntityTooLarge, on every attempt,
because no number of retries makes a file smaller.

The tests that matter most here are not "does it assemble". They are
about the parts left behind when it does not: an abandoned multipart
upload keeps its uploaded parts in the bucket, billed as storage and
invisible to any listing that looks for objects. That is the same shape
as the dead object versions that filled this bucket and took storage
down once already.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.models import SyncJob, User
from app.routes import youtube as yt


def _user(db, uid="u1"):
    u = User(
        id=uid,
        username=uid,
        email=f"{uid}@example.com",
        password_hash="$2b$12$placeholder",
        payment_status="active",
    )
    db.add(u)
    db.flush()
    return u


def _job(db, user, kind="video"):
    j = SyncJob(
        user_id=user.id, channel_id="UCx", video_id="v1", kind=kind,
        status="running", claimed_by=user.id,
    )
    db.add(j)
    db.flush()
    return j


class _FakeR2:
    """Records what the route asked object storage to do."""

    def __init__(self, complete_raises=False):
        self.calls = []
        self.complete_raises = complete_raises

    def begin_multipart(self, key, content_type=None, *, subject):
        self.calls.append(("begin", key))
        return "upload-abc"

    def presign_parts(self, key, upload_id, part_count, expires_in=0, *, subject):
        self.calls.append(("presign", part_count, expires_in))
        return [f"https://signed/{n}" for n in range(1, part_count + 1)]

    def complete_multipart(self, key, upload_id, parts, *, subject):
        self.calls.append(("complete", tuple(p["PartNumber"] for p in parts)))
        if self.complete_raises:
            raise RuntimeError("assembly blew up")

    def abort_multipart(self, key, upload_id, *, subject):
        self.calls.append(("abort", upload_id))

    def multipart_part_size(self):
        return 64 * 1024 * 1024


@pytest.fixture
def fake_r2(monkeypatch):
    fake = _FakeR2()
    monkeypatch.setattr(yt, "r2", fake)
    return fake


def test_begin_returns_a_url_per_part_and_the_size_to_split_at(db, fake_r2):
    u = _user(db)
    job = _job(db, u)

    out = yt.begin_multipart_upload(
        job_id=job.id, payload={"size": 106 * 64 * 1024 * 1024}, db=db, current=u
    )

    assert out["uploadId"] == "upload-abc"
    assert len(out["partUrls"]) == 106
    # Sent rather than hardcoded on both sides: a mismatch makes every
    # part but the last the wrong length, and S3 only says so at
    # assembly, after the whole upload has been sent.
    assert out["partSize"] == 64 * 1024 * 1024


def test_part_urls_outlive_a_long_upload(db, fake_r2):
    """7 GB on a domestic uplink is hours. A part URL that expires
    mid-transfer fails an upload that was working - which is the bug the
    single-PUT path already had and had to be widened to 6h for."""
    u = _user(db)
    job = _job(db, u)
    yt.begin_multipart_upload(job_id=job.id, payload={"size": 4 * 64 * 1024 * 1024}, db=db, current=u)

    presign = next(c for c in fake_r2.calls if c[0] == "presign")
    assert presign[2] >= 21600


def test_parts_are_assembled_in_order(monkeypatch):
    """The worker uploads parts concurrently, so it may report them out
    of order. S3 rejects an unsorted part list, so r2 sorts rather than
    trusting whatever order they arrived in.

    Tested against r2 directly, not through the route: the route hands
    the list straight down, and a fake r2 would be standing in for the
    exact code under test.
    """
    from app import r2 as real_r2

    sent = {}

    class _Boto:
        def complete_multipart_upload(self, **kw):
            sent.update(kw)

    monkeypatch.setattr(real_r2, "client", lambda: _Boto())
    monkeypatch.setattr(real_r2, "_bucket", "bucket")
    monkeypatch.setattr(real_r2, "_record", lambda *a, **k: None)

    real_r2.complete_multipart(
        "k",
        "upload-abc",
        [
            {"PartNumber": 3, "ETag": "c"},
            {"PartNumber": 1, "ETag": "a"},
            {"PartNumber": 2, "ETag": "b"},
        ],
        subject="u1",
    )

    numbers = [p["PartNumber"] for p in sent["MultipartUpload"]["Parts"]]
    assert numbers == [1, 2, 3]


def test_a_failed_assembly_aborts_rather_than_orphaning_the_parts(db, monkeypatch):
    """The expensive failure. If assembly dies and nothing aborts, every
    uploaded part stays in the bucket billing as storage and shows up in
    no object listing."""
    fake = _FakeR2(complete_raises=True)
    monkeypatch.setattr(yt, "r2", fake)
    u = _user(db)
    job = _job(db, u)

    with pytest.raises(HTTPException) as exc:
        yt.complete_multipart_upload(
            job_id=job.id,
            payload={"uploadId": "upload-abc", "parts": [{"partNumber": 1, "etag": "a"}]},
            db=db,
            current=u,
        )

    assert exc.value.status_code == 502
    assert ("abort", "upload-abc") in fake.calls, "parts were not left behind"


def test_the_worker_can_abandon_an_upload_explicitly(db, fake_r2):
    u = _user(db)
    job = _job(db, u)

    yt.abort_multipart_upload(
        job_id=job.id, payload={"uploadId": "upload-abc"}, db=db, current=u
    )

    assert ("abort", "upload-abc") in fake_r2.calls


def test_another_users_job_is_not_reachable(db, fake_r2):
    owner = _user(db, "owner")
    stranger = _user(db, "stranger")
    job = _job(db, owner)

    with pytest.raises(HTTPException) as exc:
        yt.begin_multipart_upload(
            job_id=job.id, payload={"size": 2 * 64 * 1024 * 1024}, db=db, current=stranger
        )
    assert exc.value.status_code == 404


def test_an_unclaimed_job_is_refused(db, fake_r2):
    u = _user(db)
    job = _job(db, u)
    job.claimed_by = None
    db.flush()

    with pytest.raises(HTTPException) as exc:
        yt.begin_multipart_upload(
            job_id=job.id, payload={"size": 2 * 64 * 1024 * 1024}, db=db, current=u
        )
    assert exc.value.status_code == 409


@pytest.mark.parametrize(
    "size",
    [0, -1, 10_001 * 64 * 1024 * 1024],
    ids=["zero", "negative", "past the 10000-part ceiling"],
)
def test_an_impossible_size_is_refused(db, fake_r2, size):
    """10,000 parts is S3's ceiling - 640 GB at our part size. The bound
    is here so a malformed request cannot ask us to sign an unbounded
    list of URLs."""
    u = _user(db)
    job = _job(db, u)

    with pytest.raises(HTTPException) as exc:
        yt.begin_multipart_upload(
            job_id=job.id, payload={"size": size}, db=db, current=u
        )
    assert exc.value.status_code == 400


def test_the_part_count_is_derived_from_the_size_we_were_given(db, fake_r2):
    """One owner for the part size. If both sides decided it separately,
    a mismatch makes every part but the last the wrong length and S3
    only says so at assembly, after the whole upload."""
    u = _user(db)
    job = _job(db, u)
    part = 64 * 1024 * 1024

    # 7.1 GB, the video that started this: 106 parts, last one partial.
    out = yt.begin_multipart_upload(
        job_id=job.id, payload={"size": 7_099_806_371}, db=db, current=u
    )
    assert len(out["partUrls"]) == 106

    # One byte over a boundary still needs a whole extra part.
    out = yt.begin_multipart_upload(
        job_id=job.id, payload={"size": part + 1}, db=db, current=u
    )
    assert len(out["partUrls"]) == 2


def test_multipart_is_refused_for_non_video_jobs(db, fake_r2):
    u = _user(db)
    job = _job(db, u, kind="captions")

    with pytest.raises(HTTPException) as exc:
        yt.begin_multipart_upload(
            job_id=job.id, payload={"size": 2 * 64 * 1024 * 1024}, db=db, current=u
        )
    assert exc.value.status_code == 400


# ---- the reason a person reads --------------------------------------


def test_a_failure_explains_itself_in_words_a_user_can_act_on():
    """The video list said only "Failed". The owner had to ask why one
    of his own videos had not backed up, and the answer - a 7.1 GB file
    refused by a 5 GB upload limit - was only reachable by reading job
    rows in the database."""
    from app.routes.youtube import _friendly_sync_error as explain

    assert "Too large" in explain(
        "r2 put http 400: <Code>EntityTooLarge</Code> File size too big: 7099806371"
    )
    assert "Private" in explain(
        "yt-dlp failed: ERROR: [youtube] x: Video unavailable. This video is private"
    )
    assert "has not aired" in explain(
        "ERROR: [youtube] x: This live event will begin in 3 hours."
    )


def test_an_unrecognised_failure_still_says_something_useful():
    """A reason we cannot classify must not render as an empty string -
    that puts the user back where they started, staring at "Failed"."""
    from app.routes.youtube import _friendly_sync_error as explain

    out = explain("something nobody has seen before")
    assert out and "retry" in out.lower()


def test_no_error_means_no_reason():
    from app.routes.youtube import _friendly_sync_error as explain

    assert explain(None) == ""
    assert explain("") == ""
