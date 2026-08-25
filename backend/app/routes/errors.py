"""Error reporting endpoints.

Two flows:

  POST /api/errors - the frontend (or any authenticated client) ships
  client-side uncaught errors here. window.onerror and
  window.onunhandledrejection both funnel into this. Body contains the
  message, optional stack, and optional request URL.

  GET /api/admin/errors - lives on the admin router (admin.py), not here.

Server-side exception capture happens via the FastAPI exception handler
registered in main.py - it writes ErrorLog rows for any uncaught
exception leaking out of a route. This file just handles the
client-side report submission.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ErrorLog, User, UserSession
from app.security import SESSION_COOKIE_NAME


log = logging.getLogger("archive336.errors")
router = APIRouter()


class ClientErrorReport(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    stack: Optional[str] = Field(default=None, max_length=20000)
    requestPath: Optional[str] = Field(default=None, max_length=2000)


def _resolve_user(
    db: Session, session_token: Optional[str]
) -> Optional[User]:
    """Best-effort user lookup - we don't 401 on /api/errors because
    we want to capture pre-login errors too. Returns None if no valid
    session, otherwise the User row.
    """
    if not session_token:
        return None
    sess = db.get(UserSession, session_token)
    if sess is None:
        return None
    return db.get(User, sess.user_id)


@router.post("/errors", status_code=status.HTTP_204_NO_CONTENT)
def report_client_error(
    body: ClientErrorReport,
    request: Request,
    db: Session = Depends(get_db),
    session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> Response:
    """Accept a client-side error report. Always returns 204 - we never
    want this endpoint to throw because that would silently hide errors
    from the very thing meant to capture them.
    """
    try:
        user = _resolve_user(db, session_token)
        row = ErrorLog(
            user_id=user.id if user else None,
            source="client",
            message=body.message[:4000],
            stack=body.stack[:20000] if body.stack else None,
            request_path=body.requestPath[:2000] if body.requestPath else None,
            request_method=None,
            status_code=None,
            user_agent=request.headers.get("user-agent"),
        )
        db.add(row)
        db.commit()
    except Exception:  # noqa: BLE001
        log.exception("failed to persist client error report")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
