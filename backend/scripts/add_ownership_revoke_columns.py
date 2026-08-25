"""One-off migration: add `user_revoked_at` to channel_ownerships.

`Base.metadata.create_all()` runs on boot, but it only creates missing
tables - it never ALTERs an existing one. channel_ownerships already
exists in prod, so the new column needs an explicit ALTER TABLE ADD
COLUMN.

Column added:
  - user_revoked_at: when the user deliberately revoked the worker's
    authentication for this channel. Kept separate from `revoked_at`
    because ensure_ownership() clears `revoked_at` on every worker
    ownership report (i.e. every desktop app launch), so it cannot
    hold a decision the user made. Nullable, no default - existing
    rows are all still-authenticated, which is exactly NULL.

Adds a nullable column and nothing else. No DROP, no DELETE, no data
rewrite. Idempotent - safe to re-run; checks PRAGMA table_info first.

Usage (from /opt/aether/app/backend, venv /opt/aether/venv,
env /opt/aether/.env):
    /opt/aether/venv/bin/python -m scripts.add_ownership_revoke_columns
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app.db import engine  # noqa: E402


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


def column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(r[1] == column for r in rows)


_TABLE = "channel_ownerships"

# (column_name, sqlite_type)
_COLUMNS: list[tuple[str, str]] = [
    ("user_revoked_at", "DATETIME"),
]


def main() -> None:
    with engine.begin() as conn:
        for name, ddl_type in _COLUMNS:
            if column_exists(conn, _TABLE, name):
                log.info("%s already present - skipping", name)
                continue
            log.info("adding %s (%s) to %s", name, ddl_type, _TABLE)
            conn.execute(
                text(f"ALTER TABLE {_TABLE} ADD COLUMN {name} {ddl_type}")
            )
        log.info("done")


if __name__ == "__main__":
    main()
