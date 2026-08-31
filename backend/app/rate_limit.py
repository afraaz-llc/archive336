"""Throttling for the endpoints anyone can reach without logging in.

Signup, login and password-reset were the only unauthenticated write
paths and none of them counted anything. The one that mattered most was
password reset: it is otherwise careful - always 204 so nobody can probe
which addresses exist, tokens stored only as hashes - but nothing capped
how often it would send. Anyone could POST a known address in a loop and
have us mail that person a reset link every time. That is an email bomb
sent by us, on our sending domain, at our expense, and losing that
domain's reputation would take the receipts, the verification mails and
our own operational alerts down with it.

In-process state, deliberately. The API is a single uvicorn worker (no
--workers in the unit file), which is the same assumption the export
limiter already documents. Anything multi-process needs a shared store,
and the check_state() helper at the bottom exists so that is a visible
decision rather than a silent regression.
"""
from __future__ import annotations

import time
from threading import Lock
from typing import Dict, List, Tuple

# key -> the timestamps of recent hits inside the window
_HITS: Dict[Tuple[str, str], List[float]] = {}
_LOCK = Lock()

# Above this many tracked keys, sweep the whole table instead of only
# the key being touched. Bounds memory against someone cycling through
# addresses or IPs purely to grow the dict.
_SWEEP_THRESHOLD = 10_000


def hit(bucket: str, key: str, *, limit: int, window_seconds: int) -> int:
    """Record an attempt. Returns 0 if allowed, else seconds to wait.

    A rejected attempt is NOT recorded, so hammering a limited key
    cannot keep extending its own lockout indefinitely - the window
    still drains on schedule.
    """
    if not key:
        # No usable key (no IP, no email). Fail open rather than
        # throttling everyone into one anonymous bucket, which would
        # turn a missing header into a site-wide outage.
        return 0

    now = time.time()
    cutoff = now - window_seconds
    entry = (bucket, key)

    with _LOCK:
        if len(_HITS) > _SWEEP_THRESHOLD:
            _sweep(now)

        seen = [t for t in _HITS.get(entry, ()) if t > cutoff]
        if len(seen) >= limit:
            _HITS[entry] = seen
            return max(1, int(seen[0] + window_seconds - now))

        seen.append(now)
        _HITS[entry] = seen
        return 0


def _sweep(now: float) -> None:
    """Drop keys whose newest hit is older than any window we use.

    Called with the lock held. One hour covers the longest window
    below; anything quieter than that is not rate-limited by definition.
    """
    horizon = now - 3600
    for entry in [k for k, v in _HITS.items() if not v or v[-1] < horizon]:
        _HITS.pop(entry, None)


def reset() -> None:
    """Drop all state. Tests only."""
    with _LOCK:
        _HITS.clear()


def tracked_keys() -> int:
    """How many keys are being tracked. For the admin health surface."""
    with _LOCK:
        return len(_HITS)


# ---- The limits themselves -------------------------------------------
#
# Set to be invisible to a real person and expensive for a script.
# Numbers, not opinions, so they can be argued with in one place.

# Signup, per IP. A household or small office behind one address can
# plausibly make a few accounts; nobody legitimately makes six an hour.
SIGNUP_PER_IP = (5, 3600)

# Login, per IP - and only FAILED attempts are counted. Someone using
# the app normally can sign in as often as they like; only a run of
# wrong passwords accumulates, which is the thing worth stopping.
LOGIN_FAILURES_PER_IP = (10, 900)

# Password reset. Keyed on the ADDRESS, because the abuse is aimed at
# whoever owns it rather than at us - the attacker's own IP is
# incidental and trivially rotated. One legitimate person needs one
# link; three an hour is already generous for someone who lost the
# first two.
RESET_PER_EMAIL = (3, 3600)
# And per IP, so a script cannot walk a list of addresses at speed.
RESET_PER_IP = (15, 3600)
