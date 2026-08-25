"""Side-by-side: compute_user_byte_hours (v1) vs ..._v2 on prod data.

For each user, evaluate both functions over the same window and
print the deltas. The pass criterion: every user's v2 total is
within a small tolerance of v1 (≤1% relative, ≤1e6 byte-hours
absolute - whichever is larger - to forgive the ~80-byte-per-object
metadata difference between models).

Usage:
    /opt/aether/venv/bin/python -m scripts.compare_billing_v1_v2
    /opt/aether/venv/bin/python -m scripts.compare_billing_v1_v2 --days 30
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

from app.billing import compute_user_byte_hours, compute_user_byte_hours_v2
from app.db import SessionLocal
from app.models import User


log = logging.getLogger("aether.billing_diff")


def main(argv: list[str]) -> int:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="How many days back the comparison window covers (default 30)",
    )
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    end = now
    start = now - timedelta(days=args.days)

    db = SessionLocal()
    try:
        users = db.query(User).all()
        log.info(
            "comparing v1 vs v2 byte-hours over [%s, %s] for %d users",
            start.isoformat(),
            end.isoformat(),
            len(users),
        )

        bad = 0
        for u in users:
            v1 = compute_user_byte_hours(db, u.id, start, end)
            v2 = compute_user_byte_hours_v2(db, u.id, start, end)
            delta = v2 - v1
            tol = max(1e6, abs(v1) * 0.01)  # 1% or 1e6 byte-hours
            ok = abs(delta) <= tol
            if not ok:
                bad += 1
            log.info(
                "user=%s v1=%.2f v2=%.2f delta=%+.2f (%+.4f%%) %s",
                u.id,
                v1,
                v2,
                delta,
                (delta / v1 * 100.0) if v1 else 0.0,
                "OK" if ok else "DRIFT",
            )
        if bad:
            log.error(
                "%d/%d users have drift > tolerance",
                bad,
                len(users),
            )
            return 1
        log.info("all %d users within tolerance", len(users))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
