from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from fastapi import Cookie, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User, UserSession
from app.service_access import service_is_active


SESSION_COOKIE_NAME = "archive336_session"
SESSION_LIFETIME = timedelta(days=30)

# Account switcher: the active login is the archive336_session cookie; the
# other accounts signed in on this browser are kept as a comma-separated
# list of session tokens in this second httpOnly cookie. JS never sees
# these tokens — switching is done server-side by user id. Capped so the
# cookie stays well under the 4KB limit (each token is ~43 chars).
LINKED_COOKIE_NAME = "aether_linked"
MAX_LINKED_ACCOUNTS = 8

# Worker User-Agent format set by the Tauri desktop app:
#   "ARCHIVE336-Archive-Tool-Desktop/<version> (<hostname>)"
# Capture group 1 = hostname, used to dedup worker sessions per
# device. See create_session() for usage and rationale.
_WORKER_UA_RE = re.compile(r"^ARCHIVE336-Archive-Tool-Desktop/\S+\s*\(([^)]+)\)")


def client_ip(request) -> Optional[str]:
    """The caller's real IP, as best we can tell.

    Prefer Cloudflare's CF-Connecting-IP since we sit behind it;
    X-Forwarded-For is the standard fallback; request.client is local
    dev. Shared rather than inlined because the rate limiter keys on
    this: if it disagreed with what we record on sessions, one of the
    two would be wrong about who is calling, and for the limiter that
    means either every user sharing Cloudflare's IP in one bucket or no
    limiting at all.
    """
    if request is None:
        return None
    return (
        request.headers.get("cf-connecting-ip")
        or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        or (request.client.host if request.client else None)
    )


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def create_session(
    db: Session, user: User, request: Optional[Request] = None
) -> UserSession:
    token = secrets.token_urlsafe(32)
    # Snapshot the User-Agent + remote IP at sign-in time so the
    # Sessions panel in Settings can label each row. `request` is
    # Optional so older callers that haven't been updated yet still
    # work; new sessions just get None on those columns.
    ua: Optional[str] = None
    ip: Optional[str] = None
    if request is not None:
        ua_header = request.headers.get("user-agent")
        if ua_header:
            # Truncate defensively - real UAs are well under 500 chars
            # but malicious ones can be arbitrary.
            ua = ua_header[:500]
        ip = client_ip(request)
    session = UserSession(
        token=token,
        user_id=user.id,
        expires_at=datetime.now(timezone.utc) + SESSION_LIFETIME,
        user_agent=ua,
        ip_address=ip,
    )

    # Dedup worker sessions by device hostname. The Tauri worker logs
    # in fresh every launch (its reqwest cookie store is in-memory
    # only), so without this each launch piles up another session row
    # the user has to manually revoke. Same hostname = same machine =
    # at most one active worker session at a time. We scope the cleanup
    # to UAs that match the desktop-worker pattern so a regular browser
    # session never accidentally evicts another browser session.
    if ua:
        new_match = _WORKER_UA_RE.match(ua)
        if new_match:
            new_host = new_match.group(1)
            prior_workers = (
                db.query(UserSession)
                .filter(
                    UserSession.user_id == user.id,
                    UserSession.user_agent.like(
                        "ARCHIVE336-Archive-Tool-Desktop/%"
                    ),
                )
                .all()
            )
            for prior in prior_workers:
                prior_match = _WORKER_UA_RE.match(prior.user_agent or "")
                if prior_match and prior_match.group(1) == new_host:
                    db.delete(prior)

    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=False,  # Dev only. Flip to True behind HTTPS in prod.
        max_age=int(SESSION_LIFETIME.total_seconds()),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")


def get_linked_tokens(request: Request) -> list[str]:
    """Session tokens for the *other* accounts signed in on this browser
    (the account switcher bundle). Empty list if none."""
    raw = request.cookies.get(LINKED_COOKIE_NAME) or ""
    return [t for t in raw.split(",") if t]


def set_linked_cookie(response: Response, tokens: list[str]) -> None:
    """Write the account-switcher bundle. Clears the cookie when empty.
    De-dupes, preserves order, and caps at MAX_LINKED_ACCOUNTS."""
    seen: set[str] = set()
    deduped: list[str] = []
    for t in tokens:
        if t and t not in seen:
            seen.add(t)
            deduped.append(t)
    deduped = deduped[:MAX_LINKED_ACCOUNTS]
    if not deduped:
        response.delete_cookie(key=LINKED_COOKIE_NAME, path="/")
        return
    response.set_cookie(
        key=LINKED_COOKIE_NAME,
        value=",".join(deduped),
        httponly=True,
        samesite="lax",
        secure=False,  # Dev only — match set_session_cookie.
        max_age=int(SESSION_LIFETIME.total_seconds()),
        path="/",
    )


def clear_linked_cookie(response: Response) -> None:
    response.delete_cookie(key=LINKED_COOKIE_NAME, path="/")


def resolve_session(db: Session, token: Optional[str]) -> Optional[UserSession]:
    """Return the valid (non-expired) UserSession for a token, or None.
    Expired rows are deleted, mirroring get_current_user. Used by the
    account-switcher endpoints to validate bundle tokens."""
    if not token:
        return None
    session = db.get(UserSession, token)
    if not session:
        return None
    expires = session.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        db.delete(session)
        db.commit()
        return None
    return session


def get_current_user(
    db: Session = Depends(get_db),
    session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> User:
    if not session_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )
    session = db.get(UserSession, session_token)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session"
        )
    # Note: SQLite stores datetimes without tz by default; make the compare tz-aware.
    expires = session.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        db.delete(session)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired"
        )
    user = db.get(User, session.user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Account missing"
        )
    return user


def get_admin_user(current: User = Depends(get_current_user)) -> User:
    """Like get_current_user, but additionally requires is_admin=True.

    Gates the /api/admin/* endpoints. Admin status is set manually
    via SQL — there's intentionally no "promote user" endpoint
    because that itself would need an admin to call it (chicken/egg)
    and granting admin via the UI is exactly the kind of action that
    should require thinking and database access.
    """
    if not current.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required.",
        )
    return current


def get_paid_user(current: User = Depends(get_current_user)) -> User:
    """Like get_current_user, but also requires payment_status='active'.

    Apply to endpoints that initiate new server-side work or external API
    calls (channel adds, syncs, imports). Read-only endpoints stay open so
    users can still browse what they already have. Worker-client endpoints
    (claim/heartbeat/complete/fail) also stay open — once a job is in flight,
    it should run to completion regardless of card status; any usage gets
    rolled into the next invoice attempt.

    Returns 402 Payment Required when the user hasn't completed card setup
    (status='none') or their last invoice failed (status='past_due') or
    they removed their card (status='canceled'). The frontend's fetch
    wrapper catches 402 and routes to /settings to add or fix the card.
    """
    if not service_is_active(current):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Add a payment method to use this feature.",
        )
    return current
