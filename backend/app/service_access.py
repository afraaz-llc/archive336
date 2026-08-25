"""Who is currently entitled to paid work, in one place.

There are two enforcement surfaces and they must never disagree. The HTTP
gate (``get_paid_user``) refuses requests, and the nightly crons decide
whose channels to spend money on. Until this module existed only the first
one checked anything: the rescans selected work with

    db.query(UserChannel).filter(UserChannel.removed_at.is_(None))

and no payment predicate at all, so an account the API would refuse still
had its thumbnails and avatars re-uploaded to Backblaze every single night,
forever, with nobody touching a UI. Blocking channel *creation* never
closed that, because the leak needs no user action.

The owner's rule, in their words: "a failed card should pause backups."
So the predicate is exactly ``payment_status == "active"`` - the same one
the API already used - and past_due is included in the pause rather than
carved out.

PAUSE, never delete. Nothing here removes an archive or a row; it only
stops NEW spend. Restoring service is just the column flipping back, which
is what makes this safe to apply to a transient card failure.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Set

from sqlalchemy.orm import Session

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models import User


def service_is_active(user: "User") -> bool:
    """Whether this user is entitled to paid backend work right now.

    'active' means an invoice has actually been paid (see the webhook in
    routes/billing.py). The other states are all reasons to pause:
    'none' never paid, 'past_due' had a payment fail, 'canceled' stopped.
    """
    return user.payment_status == "active"


def active_service_user_ids(db: Session) -> Set[str]:
    """Ids of every user currently entitled to paid work.

    For the batch jobs, which decide across all users at once and would
    otherwise need a row-by-row lookup. Returned as a set so callers can
    filter an existing query with ``.in_()`` without a second join.
    """
    from app.models import User  # local import: avoids a circular at load

    return {
        uid
        for (uid,) in db.query(User.id).filter(User.payment_status == "active")
    }
