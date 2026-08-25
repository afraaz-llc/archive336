"""Frontend test helper — flip a channel's youtubeStatus between
'available' and 'terminated' so you can eyeball the terminated-channel
indicator on the dashboard and detail page.

Per the no-server-deletion rule, this only edits the channelStatus flag;
all videos / metadata / etc stay intact.

Usage:
    .venv/bin/python -m scripts.toggle_terminated [@handle_or_uc_id]

Defaults to @Afraaz if no argument is given.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


DB_PATH = Path(__file__).resolve().parent.parent / "aether.db"


def main() -> None:
    channel_id = sys.argv[1] if len(sys.argv) > 1 else "@Afraaz"
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT user_id, data_json FROM user_channels WHERE channel_id=?",
        (channel_id,),
    ).fetchall()
    if not rows:
        print(f"No channel rows found for {channel_id!r}")
        return

    for user_id, raw in rows:
        data = json.loads(raw)
        prev = data.get("youtubeStatus", "available")
        if prev == "terminated":
            data["youtubeStatus"] = "available"
            data["terminatedAt"] = None
            print(f"  user={user_id[:8]}…  terminated → available  (cleared terminatedAt)")
        else:
            data["youtubeStatus"] = "terminated"
            data["terminatedAt"] = datetime.now(timezone.utc).isoformat()
            print(f"  user={user_id[:8]}…  available  → terminated  ({data['terminatedAt']})")
        conn.execute(
            "UPDATE user_channels SET data_json=? WHERE user_id=? AND channel_id=?",
            (json.dumps(data), user_id, channel_id),
        )
    conn.commit()
    print(f"\nReload the dashboard / channel page to see the indicator change.")


if __name__ == "__main__":
    main()
