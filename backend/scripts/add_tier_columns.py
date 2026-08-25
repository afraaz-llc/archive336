"""One-off migration: add `tier` + `tier_override` columns to users.

SQLAlchemy's `Base.metadata.create_all()` runs on boot but won't
modify existing tables. New columns on `users` need an explicit
ALTER TABLE ADD COLUMN.

Idempotent - safe to re-run; checks PRAGMA table_info first.

Usage:
    /opt/aether/venv/bin/python -m scripts.add_tier_columns
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


# (column_name, sqlite_type, default_clause_or_None)
# `tier` gets a DEFAULT 'basic' so existing rows backfill to the only
# tier wired in code today. `tier_override` is nullable, no default.
_COLUMNS: list[tuple[str, str, str | None]] = [
    ("tier", "VARCHAR", "'basic'"),
    ("tier_override", "VARCHAR", None),
]


def main() -> None:
    with engine.begin() as conn:
        for name, ddl_type, default in _COLUMNS:
            if column_exists(conn, "users", name):
                log.info("%s already present - skipping", name)
                continue
            ddl = f"ALTER TABLE users ADD COLUMN {name} {ddl_type}"
            if default is not None:
                ddl += f" NOT NULL DEFAULT {default}"
            log.info("adding %s to users", name)
            conn.execute(text(ddl))
        log.info("done")


if __name__ == "__main__":
    main()
