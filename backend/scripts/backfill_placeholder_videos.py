"""One-off: give pool rows to videos that only ever existed in the
legacy per-user table.

Pool ``Video`` rows were only ever written by a successful sync or by a
PubSub notice about a public upload. A video that failed on every
attempt therefore had no pool row, and since every listing reads the
pool, it could not be shown anywhere - while the home page's failure
banner, which counts jobs rather than videos, went on counting it.

``fail_sync_job`` now creates the row as the failure is recorded, so
this only needs to run once, to catch the ones that failed before that
existed.

Run:  PYTHONPATH=/opt/aether/app/backend python scripts/backfill_placeholder_videos.py [--apply]

Without --apply it reports what it would do and changes nothing.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from app import archive
from app.db import SessionLocal
from app.models import Channel, UserChannelVideo, Video


def main(apply: bool) -> int:
    db = SessionLocal()
    try:
        pool_ids = {v for (v,) in db.query(Video.youtube_id)}
        channels = {c.youtube_id: c for c in db.query(Channel)}

        created = 0
        skipped_no_channel = 0
        for row in db.query(UserChannelVideo):
            if row.video_id in pool_ids:
                continue
            channel = channels.get(row.channel_id)
            if channel is None:
                # The channel itself never made it into the pool. Nothing
                # to attach the video to, and inventing a channel here
                # would be a bigger claim than this script should make.
                skipped_no_channel += 1
                continue

            try:
                data = json.loads(row.data_json) or {}
            except (json.JSONDecodeError, TypeError):
                data = {}

            title = data.get("title") or row.video_id
            privacy = data.get("privacy")
            # Carry the real upload date across. Without it the row gets
            # "now", which is the moment this script ran - wrong for
            # anything that sorts or reasons on the pool row itself.
            published = None
            raw = data.get("uploadDate")
            if isinstance(raw, str) and raw:
                try:
                    published = datetime.fromisoformat(
                        raw.replace("Z", "+00:00")
                    )
                    if published.tzinfo is None:
                        published = published.replace(tzinfo=timezone.utc)
                except ValueError:
                    published = None
            print(
                f"  {row.video_id}  {(title or '')[:44]:46} "
                f"channel={(channel.title or '?')[:14]:16} privacy={privacy} "
                f"published={published.date() if published else 'unknown'}"
            )
            if apply:
                archive.ensure_placeholder_video(
                    db,
                    channel=channel,
                    youtube_video_id=row.video_id,
                    title=title,
                    privacy=privacy,
                    published_at=published,
                )
                pool_ids.add(row.video_id)
            created += 1

        print()
        print(f"{'created' if apply else 'would create'}: {created}")
        if skipped_no_channel:
            print(f"skipped (channel not in pool): {skipped_no_channel}")

        if apply:
            db.commit()
            print("committed")
        else:
            print("dry run - pass --apply to write")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main("--apply" in sys.argv))
