"""One-off backfill: probe + hash every already-archived video.

When we added ffprobe/SHA-256 capture in the desktop worker, only
videos synced AFTER that change ended up with the advanced fields
(videoCodec, videoFps, audioCodec, audioBitrateKbps, videoBitrateKbps,
videoFormat, fileSha256). This script fixes the gap for everything
synced earlier by re-reading each mp4 from R2 and stamping the
metadata back onto the row - no YouTube traffic, no worker app
involvement, no impact on the user-facing file (we never write the
mp4 back, only update the JSON blob in the DB).

Run on the server (which needs ffmpeg installed - apt install ffmpeg).

Usage:
    /opt/aether/venv/bin/python -m scripts.backfill_file_metadata
    /opt/aether/venv/bin/python -m scripts.backfill_file_metadata --dry
    /opt/aether/venv/bin/python -m scripts.backfill_file_metadata --force
        # ^ re-probes even rows that already have the new fields

Idempotent: by default skips any row that already has fileSha256 +
videoCodec set. Use --force to re-probe everything (useful only if
the schema for what we extract changes).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
from typing import Any, Dict, Optional

# Allow `python -m scripts.backfill_file_metadata` from /opt/aether/app/backend
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import r2  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import UserChannelVideo  # noqa: E402


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


def probe_mp4(path: str) -> Dict[str, Any]:
    """Same field extraction as the desktop worker's probe_mp4 in Rust.
    Returns a partial dict of camelCase keys that the rest of the
    code stamps onto the video's data_json.
    """
    out: Dict[str, Any] = {}
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_streams",
                "-show_format",
                path,
            ],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        log.error(
            "ffprobe not found on PATH - install with: apt install ffmpeg"
        )
        sys.exit(2)

    if proc.returncode != 0:
        log.warning("ffprobe non-zero exit: %s", proc.stderr.decode(errors="replace"))
        return out

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        log.warning("ffprobe json parse failed: %s", e)
        return out

    fmt = data.get("format") or {}
    fmt_name = fmt.get("format_name")
    if isinstance(fmt_name, str) and fmt_name:
        out["videoFormat"] = fmt_name.split(",")[0]

    for stream in data.get("streams") or []:
        kind = stream.get("codec_type")
        if kind == "video" and "videoCodec" not in out:
            codec = stream.get("codec_name")
            if codec:
                out["videoCodec"] = codec
            w = stream.get("width")
            h = stream.get("height")
            if isinstance(w, int) and isinstance(h, int):
                out["videoResolution"] = f"{w}x{h}"
            br = stream.get("bit_rate")
            if isinstance(br, str):
                try:
                    out["videoBitrateKbps"] = int(br) // 1000
                except ValueError:
                    pass
            rate = stream.get("r_frame_rate")
            if isinstance(rate, str) and "/" in rate:
                n, d = rate.split("/", 1)
                try:
                    nf = float(n)
                    df = float(d)
                    if df > 0:
                        out["videoFps"] = round(nf / df, 1)
                except ValueError:
                    pass
        elif kind == "audio" and "audioCodec" not in out:
            codec = stream.get("codec_name")
            if codec:
                out["audioCodec"] = codec
            br = stream.get("bit_rate")
            if isinstance(br, str):
                try:
                    out["audioBitrateKbps"] = int(br) // 1000
                except ValueError:
                    pass
    return out


def hash_file_sha256(path: str) -> str:
    """Stream the file through a SHA-256 hasher. 64KB chunks so big
    videos don't sit in memory."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download_from_r2(key: str, dest_path: str) -> None:
    """Stream an R2 object to a local file via boto3's get_object body
    iterator. Avoids loading the entire mp4 into memory."""
    client = r2.client()
    if client is None or r2._bucket is None:
        raise RuntimeError("R2 client not configured (check env vars)")
    obj = client.get_object(Bucket=r2._bucket, Key=key)
    body = obj["Body"]
    with open(dest_path, "wb") as f:
        for chunk in body.iter_chunks(chunk_size=64 * 1024):
            f.write(chunk)


def needs_backfill(data: Dict[str, Any], force: bool) -> bool:
    """A row needs backfill if it's archived AND missing at least one
    of the new fields, OR if --force is set. We treat fileSha256 +
    videoCodec as the canary; if either is missing, we re-probe."""
    if data.get("status") != "archived":
        return False
    if not data.get("localPath"):
        return False
    if force:
        return True
    return not (data.get("fileSha256") and data.get("videoCodec"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill ffprobe + hash metadata.")
    parser.add_argument("--dry", action="store_true", help="Report only; don't write to DB.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-probe rows that already have the new fields.",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        rows = db.query(UserChannelVideo).all()
        log.info("scanning %d UserChannelVideo rows", len(rows))

        candidates = []
        for row in rows:
            try:
                data = json.loads(row.data_json)
            except json.JSONDecodeError:
                continue
            if needs_backfill(data, args.force):
                candidates.append((row, data))

        log.info(
            "%d videos need backfill (dry=%s, force=%s)",
            len(candidates),
            args.dry,
            args.force,
        )
        if args.dry:
            for row, data in candidates:
                log.info("  would backfill %s (%s)", row.video_id, data.get("title", ""))
            return

        for i, (row, data) in enumerate(candidates, 1):
            key = data.get("localPath")
            log.info("[%d/%d] %s -> %s", i, len(candidates), row.video_id, key)

            tmp_path: Optional[str] = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                    tmp_path = tmp.name
                try:
                    download_from_r2(key, tmp_path)
                except Exception as e:
                    log.warning("  ! download failed: %s", e)
                    continue

                size = os.path.getsize(tmp_path)
                log.info("  downloaded %s bytes", f"{size:,}")

                probed = probe_mp4(tmp_path)
                probed["fileSha256"] = hash_file_sha256(tmp_path)
                log.info(
                    "  probed: codec=%s res=%s vbr=%s fps=%s acodec=%s abr=%s sha=%s",
                    probed.get("videoCodec"),
                    probed.get("videoResolution"),
                    probed.get("videoBitrateKbps"),
                    probed.get("videoFps"),
                    probed.get("audioCodec"),
                    probed.get("audioBitrateKbps"),
                    (probed.get("fileSha256") or "")[:8],
                )

                # Merge - never overwrite an existing non-null field with
                # a null one. The probe might return partial data; that
                # shouldn't wipe what we already have.
                for k, v in probed.items():
                    if v is not None:
                        data[k] = v
                row.data_json = json.dumps(data)
                db.commit()
                log.info("  saved")
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)
    finally:
        db.close()


if __name__ == "__main__":
    main()
