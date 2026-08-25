"""ARCHIVE336 sync worker — PUBLIC-ONLY FALLBACK PATH.

⚠️  READ THIS BEFORE TOUCHING THIS FILE  ⚠️

This is NOT the MVP worker. The MVP worker is the Tauri desktop app
in ``/desktop/`` — it runs on the user's Mac with their logged-in
Chrome cookies and can pull private/unlisted/own-channel videos. This
file, by contrast, runs as ``archive336-worker.service`` on the Hetzner
VM with no cookies and no OAuth tokens in its yt-dlp options, so it
can only download genuinely public videos. Private videos hit this
file's code path will fail with "Video unavailable" / 403.

If you're debugging "why doesn't private-video download work," the
answer is "make sure the desktop app picks the job up, not this
worker." See ARCHITECTURE.md → "CRITICAL: the desktop app is the
real MVP worker" for the full story.

Mechanics of this file:

Long-running process that drains the SyncJob queue, runs yt-dlp on each
video, and uploads the resulting .mp4 to R2. Supervised by systemd as
``archive336-worker.service``.

Single-threaded on purpose — yt-dlp + ffmpeg are already CPU/disk-heavy
and the CPX11 box has 2 vCPUs and 2 GB of RAM, so running multiple jobs
in parallel would just thrash the box. We can revisit if/when the worker
has serious headroom.

Crash safety: on startup we reset any rows stuck in ``running`` back to
``pending`` so the next loop iteration picks them up. yt-dlp is naturally
resumable but we don't take advantage of that yet — partial files in
/tmp from a crashed job are just thrown away.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yt_dlp  # type: ignore[import-untyped]

from app import r2_paths, storage_ledger
from app.db import SessionLocal, engine
from app.db import Base
from app.models import SyncJob, UserChannelVideo
from app.r2 import upload_file


log = logging.getLogger("archive336.worker")

POLL_INTERVAL_SEC = 5
PROGRESS_WRITE_INTERVAL_SEC = 1.5  # don't pound SQLite


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _reset_zombie_jobs() -> int:
    """If a previous worker died mid-job, those rows are stuck in 'running'.
    Reset them to pending on startup so we pick them up again."""
    db = SessionLocal()
    try:
        n = (
            db.query(SyncJob)
            .filter(SyncJob.status == "running")
            .update(
                {"status": "pending", "started_at": None, "progress": 0.0},
                synchronize_session=False,
            )
        )
        db.commit()
        return n
    finally:
        db.close()


def _next_pending_job() -> Optional[SyncJob]:
    db = SessionLocal()
    try:
        return (
            db.query(SyncJob)
            .filter(SyncJob.status == "pending")
            .order_by(SyncJob.created_at)
            .first()
        )
    finally:
        db.close()


def _claim(job_id: str) -> bool:
    """Atomically flip pending→running. Returns False if someone beat us."""
    db = SessionLocal()
    try:
        n = (
            db.query(SyncJob)
            .filter(SyncJob.id == job_id, SyncJob.status == "pending")
            .update(
                {"status": "running", "started_at": _now(), "progress": 0.0},
                synchronize_session=False,
            )
        )
        db.commit()
        return n > 0
    finally:
        db.close()


def _write_progress(job_id: str, progress: float) -> None:
    db = SessionLocal()
    try:
        db.query(SyncJob).filter(SyncJob.id == job_id).update(
            {"progress": max(0.0, min(progress, 0.99))},
            synchronize_session=False,
        )
        db.commit()
    finally:
        db.close()


def _finalize_done(job_id: str, r2_key: str, file_size: int) -> None:
    db = SessionLocal()
    try:
        job = db.query(SyncJob).filter(SyncJob.id == job_id).first()
        if not job:
            return
        job.status = "done"
        job.progress = 1.0
        job.r2_key = r2_key
        job.file_size_bytes = file_size
        job.finished_at = _now()

        # Update the linked Video row so the frontend sees status=archived
        # without having to hit the worker tables. We mutate the JSON blob
        # in place since the frontend owns the schema.
        video = (
            db.query(UserChannelVideo)
            .filter_by(
                user_id=job.user_id,
                channel_id=job.channel_id,
                video_id=job.video_id,
            )
            .first()
        )
        if video:
            try:
                data = json.loads(video.data_json)
            except json.JSONDecodeError:
                data = {}
            data["status"] = "archived"
            data["localPath"] = r2_key
            data["fileSizeBytes"] = file_size
            data["archivedAt"] = _now().isoformat()
            data.pop("syncProgress", None)
            video.data_json = json.dumps(data)

        db.commit()
    finally:
        db.close()


def _finalize_failed(job_id: str, error: str) -> None:
    db = SessionLocal()
    try:
        job = db.query(SyncJob).filter(SyncJob.id == job_id).first()
        if not job:
            return
        job.status = "failed"
        job.error = error[:500]
        job.finished_at = _now()

        # Mirror the failure onto the video row so the UI shows it.
        video = (
            db.query(UserChannelVideo)
            .filter_by(
                user_id=job.user_id,
                channel_id=job.channel_id,
                video_id=job.video_id,
            )
            .first()
        )
        if video:
            try:
                data = json.loads(video.data_json)
            except json.JSONDecodeError:
                data = {}
            data["status"] = "failed"
            data.pop("syncProgress", None)
            video.data_json = json.dumps(data)

        db.commit()
    finally:
        db.close()


def _mark_video_syncing(job: SyncJob) -> None:
    """Flip the linked Video row's status to 'syncing' so the UI shows progress."""
    db = SessionLocal()
    try:
        video = (
            db.query(UserChannelVideo)
            .filter_by(
                user_id=job.user_id,
                channel_id=job.channel_id,
                video_id=job.video_id,
            )
            .first()
        )
        if not video:
            return
        try:
            data = json.loads(video.data_json)
        except json.JSONDecodeError:
            data = {}
        data["status"] = "syncing"
        data["syncProgress"] = 0.0
        video.data_json = json.dumps(data)
        db.commit()
    finally:
        db.close()


def _run_ytdlp(youtube_id: str, out_dir: Path, job_id: str) -> Path:
    """Download the video to ``out_dir`` and return the resulting .mp4 path."""
    last_progress_write = 0.0

    def hook(d):
        nonlocal last_progress_write
        if d.get("status") != "downloading":
            return
        downloaded = d.get("downloaded_bytes") or 0
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        if total <= 0:
            return
        # yt-dlp's "downloading" pass actually runs twice (video stream then
        # audio stream). Treat the first as 0-0.5 and the second as 0.5-1.
        # We don't have a reliable way to tell which stream we're on, so
        # just clamp the per-stream progress to 0.95 to leave room for the
        # ffmpeg merge step at the end.
        per_stream = min(downloaded / total, 0.95)
        # Cap at 0.95 overall too — the upload step will move 0.95→1.0.
        progress = min(per_stream * 0.5, 0.95)

        now = time.monotonic()
        if now - last_progress_write >= PROGRESS_WRITE_INTERVAL_SEC:
            _write_progress(job_id, progress)
            last_progress_write = now

    output_template = str(out_dir / "video.%(ext)s")
    ydl_opts = {
        # Best mp4 video + m4a audio under 1080p, fall back to whatever's available
        "format": "bv*[ext=mp4][height<=1080]+ba[ext=m4a]/best[ext=mp4][height<=1080]/best",
        "merge_output_format": "mp4",
        "outtmpl": output_template,
        "progress_hooks": [hook],
        "no_warnings": True,
        "quiet": True,
        "noprogress": True,
        # Don't bail on geo restrictions if we can use an alternate route
        "geo_bypass": True,
    }

    youtube_url = f"https://www.youtube.com/watch?v={youtube_id}"
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([youtube_url])

    mp4s = list(out_dir.glob("*.mp4"))
    if not mp4s:
        raise RuntimeError("yt-dlp produced no .mp4 output")
    return mp4s[0]


def _process_job(job: SyncJob) -> None:
    log.info("starting job %s (video=%s)", job.id, job.video_id)
    if not _claim(job.id):
        log.info("job %s claimed by someone else", job.id)
        return
    _mark_video_syncing(job)

    r2_key = r2_paths.video_key(job.user_id, job.video_id)
    try:
        upload_metadata_bytes = 0
        with tempfile.TemporaryDirectory(prefix="aether-sync-") as tmp:
            mp4_path = _run_ytdlp(job.video_id, Path(tmp), job.id)
            file_size = mp4_path.stat().st_size
            log.info("uploading %s bytes to R2 at %s", f"{file_size:,}", r2_key)
            _write_progress(job.id, 0.95)
            upload_metadata_bytes = upload_file(
                str(mp4_path),
                r2_key,
                content_type="video/mp4",
                subject=job.user_id,
            )
        _finalize_done(job.id, r2_key, file_size)
        # Record the upload in the storage ledger. The desktop worker
        # path goes through the API route (which does this), but the
        # server-side worker bypasses the API — wire it directly here.
        ledger_db = SessionLocal()
        try:
            storage_ledger.record_object(
                ledger_db,
                user_id=job.user_id,
                r2_key=r2_key,
                byte_count=file_size,
                kind="video",
                metadata_bytes=upload_metadata_bytes,
            )
            ledger_db.commit()
        finally:
            ledger_db.close()
        log.info("job %s done (%s, %s bytes)", job.id, r2_key, f"{file_size:,}")
    except Exception as e:  # noqa: BLE001 — we want to catch anything yt-dlp throws
        log.exception("job %s failed", job.id)
        _finalize_failed(job.id, repr(e))


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    log.info("aether worker starting")

    Base.metadata.create_all(bind=engine)

    n = _reset_zombie_jobs()
    if n:
        log.info("reset %s zombie jobs back to pending", n)

    while True:
        job = _next_pending_job()
        if job is None:
            time.sleep(POLL_INTERVAL_SEC)
            continue
        _process_job(job)
    # unreachable
    return 0


if __name__ == "__main__":
    sys.exit(main())
