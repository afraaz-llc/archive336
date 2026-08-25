"""Support conversation endpoints.

One thread per user, both directions. The user writes from the Support
page; the maintainer replies from the admin panel. Every user message
carries an account snapshot taken at send time (see app.support) because
the questions people ask about a backup tool are questions about state
they cannot see.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import SupportMessage, User
from app.security import get_admin_user, get_current_user
from app import support as support_lib

log = logging.getLogger("archive336.support")

router = APIRouter(prefix="/api/support", tags=["support"])


def _serialize(m: SupportMessage, *, include_snapshot: bool = False) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "id": m.id,
        "kind": m.kind,
        "body": m.body,
        "fromStaff": m.from_staff,
        "createdAt": m.created_at.isoformat() if m.created_at else None,
    }
    # The snapshot is for the maintainer. Handing a user their own
    # diagnostic dump would invite them to debug it, which is the job
    # this feature exists to take off them.
    if include_snapshot and m.snapshot_json:
        try:
            out["snapshot"] = json.loads(m.snapshot_json)
        except (TypeError, ValueError):
            pass
    return out


@router.get("/thread")
def my_thread(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """This user's conversation, oldest first."""
    return {
        "messages": [_serialize(m) for m in support_lib.thread_for(db, current.id)]
    }


@router.post("/messages", status_code=status.HTTP_201_CREATED)
def post_message(
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    body = str(payload.get("body") or "").strip()
    kind = str(payload.get("kind") or "question").strip()
    if not body:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Write a message first."
        )
    if len(body) > support_lib.MAX_BODY_CHARS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Keep it under {support_lib.MAX_BODY_CHARS} characters.",
        )
    if kind not in support_lib.SUPPORT_KINDS:
        kind = "question"

    snapshot = support_lib.account_snapshot(db, current)
    msg = SupportMessage(
        user_id=current.id,
        kind=kind,
        body=body,
        from_staff=False,
        snapshot_json=json.dumps(snapshot),
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)

    # Best-effort: a failed notification must not lose the message the
    # user just wrote. It is in the database either way, and the admin
    # inbox is the durable path - email is only the nudge.
    try:
        from app.email import send_support_message_alert

        send_support_message_alert(
            username=current.username,
            kind=kind,
            body=body,
            snapshot_text=support_lib.snapshot_lines(snapshot),
        )
    except Exception:
        log.exception("support alert email failed")

    return _serialize(msg)


# ---------- maintainer side ----------


@router.get("/admin/threads")
def admin_threads(
    db: Session = Depends(get_db),
    _admin: User = Depends(get_admin_user),
) -> Dict[str, Any]:
    """Every conversation, most recently active first."""
    rows = (
        db.query(SupportMessage, User)
        .join(User, User.id == SupportMessage.user_id)
        .order_by(SupportMessage.created_at.desc())
        .all()
    )
    threads: Dict[str, Dict[str, Any]] = {}
    for m, u in rows:
        t = threads.setdefault(
            u.id,
            {
                "userId": u.id,
                "username": u.username,
                "email": u.email,
                "lastAt": m.created_at.isoformat() if m.created_at else None,
                # Awaiting a reply when the newest message is theirs.
                "awaitingReply": not m.from_staff,
                "messages": [],
            },
        )
        t["messages"].append(_serialize(m, include_snapshot=True))
    for t in threads.values():
        t["messages"].reverse()
    return {"threads": list(threads.values())}


@router.post("/admin/threads/{user_id}/reply", status_code=status.HTTP_201_CREATED)
def admin_reply(
    user_id: str,
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
    _admin: User = Depends(get_admin_user),
) -> Dict[str, Any]:
    body = str(payload.get("body") or "").strip()
    if not body:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Write a reply first."
        )
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such user."
        )

    msg = SupportMessage(
        user_id=user_id, kind="question", body=body, from_staff=True
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)

    try:
        from app.email import send_support_reply

        send_support_reply(to_email=user.email, body=body)
    except Exception:
        log.exception("support reply email failed")

    return _serialize(msg)
