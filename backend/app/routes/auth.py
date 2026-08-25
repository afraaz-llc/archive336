from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import time
from threading import Lock

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session
from pydantic import BaseModel

import stripe

from app import billing as billing_lib
from app import email as email_lib
from app import encryption, google_oauth, r2, storage_ledger, tiers
from app.db import get_db
from app.models import (
    AccountDeletionToken,
    Channel,
    ChannelOwnership,
    EmailSendLog,
    EmailVerificationToken,
    PasswordResetToken,
    UsageRecord,
    User,
    UserChannel,
    UserChannelVideo,
    Video,
    UserGoogleConnection,
    UserSession,
    UserUiPrefs,
    UserYouTubeSettings,
    WorkerYoutubeConnection,
)
from app.schemas import (
    ConfirmAccountDeletionRequest,
    ForgotPasswordRequest,
    LoginRequest,
    RequestAccountDeletionRequest,
    ResetPasswordRequest,
    SignupRequest,
    UpdateProfileRequest,
    UserOut,
    VerifyEmailRequest,
)
from app.security import (
    SESSION_COOKIE_NAME,
    clear_linked_cookie,
    clear_session_cookie,
    create_session,
    get_current_user,
    get_linked_tokens,
    hash_password,
    resolve_session,
    set_linked_cookie,
    set_session_cookie,
    verify_password,
)


# Password-reset tokens expire 1 hour after issue. Long enough for a
# user to actually find the email and click; short enough to limit
# the window of risk if a reset link is intercepted.
PASSWORD_RESET_TTL = timedelta(hours=1)

# Email-verification tokens expire 7 days after issue — verification
# is less time-sensitive than password reset.
EMAIL_VERIFY_TTL = timedelta(days=7)

# How often the user can request a fresh verification email. Matches
# the UI countdown in AccountEditor.
EMAIL_VERIFY_RESEND_COOLDOWN = timedelta(hours=1)

# Account-deletion email link is valid for an hour. Long enough that
# a user finishing dinner mid-flow can still confirm; short enough
# that an intercepted link goes stale fast.
ACCOUNT_DELETION_TTL = timedelta(hours=1)


def _issue_verification_token(
    db: Session, user: User, request: Request
) -> None:
    """Generate a fresh verification token, persist its hash, send the
    email. Best-effort on the email send — logged-and-swallowed so a
    Resend hiccup doesn't break the calling flow (signup, resend, etc).
    """
    plaintext = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    db.add(
        EmailVerificationToken(
            token_hash=token_hash,
            user_id=user.id,
            expires_at=datetime.now(timezone.utc) + EMAIL_VERIFY_TTL,
        )
    )
    db.commit()

    origin = request.headers.get("origin") or os.environ.get(
        "ARCHIVE336_FRONTEND_ORIGIN", "https://archive336.com"
    )
    verify_url = (
        f"{origin.rstrip('/')}/verify-email?{urlencode({'token': plaintext})}"
    )
    try:
        email_lib.send_email_verification(user.email, verify_url)
        db.add(EmailSendLog(type="verification", to_email=user.email))
        db.commit()
    except Exception:
        log.exception(
            "failed to send verification email to user %s", user.id
        )


log = logging.getLogger("archive336.auth")


router = APIRouter()


def _preserve_current_in_switcher(
    db: Session,
    request: Request,
    response: Response,
    new_user_id: str,
) -> None:
    """For "add another account" mode: keep the currently-active login in
    the account-switcher bundle instead of replacing it, and drop any stale
    bundle entry for the account we just signed into / created so it isn't
    listed twice. Shared by login(add=true) and signup(add=true)."""
    linked: list[str] = []
    for t in get_linked_tokens(request):
        rs = resolve_session(db, t)
        if rs and rs.user_id != new_user_id:
            linked.append(t)
    current = request.cookies.get(SESSION_COOKIE_NAME)
    cur_session = resolve_session(db, current)
    if cur_session and cur_session.user_id != new_user_id:
        linked.insert(0, current)
    set_linked_cookie(response, linked)


@router.post("/signup", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def signup(
    payload: SignupRequest,
    request: Request,
    response: Response,
    add: bool = False,
    db: Session = Depends(get_db),
) -> User:
    existing = (
        db.query(User)
        .filter(or_(User.username == payload.username, User.email == payload.email))
        .first()
    )
    if existing:
        if existing.username == payload.username:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Username already taken.",
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account already exists for that email.",
        )

    user = User(
        username=payload.username,
        email=payload.email,
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # Fire the first verification email automatically. The user can
    # resend from Settings if it gets lost.
    _issue_verification_token(db, user, request)

    session = create_session(db, user, request)
    if add:
        # Signed up as an ADDITIONAL account from the account switcher —
        # keep the current account signed in instead of replacing it.
        _preserve_current_in_switcher(db, request, response, user.id)
    set_session_cookie(response, session.token)
    return user


@router.post("/login", response_model=UserOut)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    add: bool = False,
    db: Session = Depends(get_db),
) -> User:
    # The login identifier field accepts EITHER a username or an email
    # address. Username match is exact; email match is case-insensitive
    # (emails are case-insensitive in practice, and we store them as
    # the user typed at signup, so normalize both sides for the compare).
    from sqlalchemy import func  # noqa: WPS433

    identifier = (payload.username or "").strip()
    user = (
        db.query(User)
        .filter(
            or_(
                User.username == identifier,
                func.lower(User.email) == identifier.lower(),
            )
        )
        .first()
    )
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect credentials",
        )

    session = create_session(db, user, request)

    if add:
        # "Add another account" (account switcher): keep the currently-
        # active login in the switcher bundle instead of replacing it.
        _preserve_current_in_switcher(db, request, response, user.id)

    set_session_cookie(response, session.token)
    return user


@router.post("/forgot-password", status_code=status.HTTP_204_NO_CONTENT)
def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Send a password reset link to the email if it matches an account.

    Always returns 204 regardless of whether the email is on file —
    that way attackers can't probe for which addresses are registered.
    Email send failures are logged but also produce 204 to the user.
    """
    user = db.query(User).filter(User.email == payload.email).first()
    if user is None:
        log.info("forgot-password: no account for %s (returning 204)", payload.email)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # Generate a token. Plaintext goes in the email; only the hash
    # is stored. URL-safe so it works as a query string param.
    plaintext = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()

    record = PasswordResetToken(
        token_hash=token_hash,
        user_id=user.id,
        expires_at=datetime.now(timezone.utc) + PASSWORD_RESET_TTL,
    )
    db.add(record)
    db.commit()

    # Build the reset URL using the request origin (so a self-hosted
    # user is sent back to their own host) with a production fallback.
    origin = request.headers.get("origin") or os.environ.get(
        "ARCHIVE336_FRONTEND_ORIGIN", "https://archive336.com"
    )
    reset_url = f"{origin.rstrip('/')}/reset-password?{urlencode({'token': plaintext})}"

    try:
        email_lib.send_password_reset(user.email, reset_url)
        db.add(EmailSendLog(type="password_reset", to_email=user.email))
        db.commit()
    except Exception:
        log.exception(
            "failed to send password reset email to user %s", user.id
        )
        # Still 204 — user shouldn't see internal email failures.

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
def reset_password(
    payload: ResetPasswordRequest,
    db: Session = Depends(get_db),
) -> Response:
    """Redeem a reset token and set a new password.

    Validates the token (exists, not used, not expired), updates the
    user's password_hash, marks the token used, and invalidates ALL
    existing sessions so a compromised attacker session can't survive
    the reset. The user has to log in again everywhere afterward.
    """
    token_hash = hashlib.sha256(payload.token.encode("utf-8")).hexdigest()
    record = db.get(PasswordResetToken, token_hash)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This reset link is invalid or has already been used.",
        )

    if record.used_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This reset link has already been used. Request a new one.",
        )

    expires = record.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This reset link has expired. Request a new one.",
        )

    user = db.get(User, record.user_id)
    if user is None:
        # Account was deleted between issuing the token and redeeming.
        # Treat as invalid rather than 500 — same UX as expired link.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This reset link is invalid or has already been used.",
        )

    user.password_hash = hash_password(payload.new_password)
    record.used_at = datetime.now(timezone.utc)

    # Invalidate every existing session for this user — security best
    # practice after a password reset. The user (and any attacker who
    # had a stolen session) must log in fresh with the new password.
    db.query(UserSession).filter(UserSession.user_id == user.id).delete(
        synchronize_session=False
    )

    db.commit()
    log.info("password reset for user %s", user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/verify-email", status_code=status.HTTP_204_NO_CONTENT)
def verify_email(
    payload: VerifyEmailRequest,
    db: Session = Depends(get_db),
) -> Response:
    """Redeem a verification token, marking the matched user's email
    as verified.

    Public endpoint — having the token IS the auth (because only
    someone who can read the inbox could have it). User does not
    need to be logged in to redeem.
    """
    token_hash = hashlib.sha256(payload.token.encode("utf-8")).hexdigest()
    record = db.get(EmailVerificationToken, token_hash)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This verification link is invalid.",
        )
    if record.used_at is not None:
        # Idempotent — double-clicking the link is a normal user
        # behavior, not an error. Just confirm success without flipping
        # used_at twice or doing anything else.
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    expires = record.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This verification link has expired. Request a new one from Settings.",
        )

    user = db.get(User, record.user_id)
    if user is None:
        # Account deleted between issuing and redeeming.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This verification link is invalid.",
        )

    user.email_verified = True
    record.used_at = datetime.now(timezone.utc)
    db.commit()
    log.info("user %s verified email %s", user.id, user.email)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/verify-cooldown")
def verify_cooldown(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> dict:
    """Return when the user can next request a verification email.

    `resendAvailableAt` is an ISO timestamp if the cooldown is active,
    or null if the user can request right now (or is already verified
    and never needs to). Lets the UI render the correct disabled state
    even after a fresh page load where localStorage doesn't have the
    last-sent timestamp.
    """
    if current.email_verified:
        return {"resendAvailableAt": None}
    recent = (
        db.query(EmailVerificationToken)
        .filter(EmailVerificationToken.user_id == current.id)
        .order_by(EmailVerificationToken.created_at.desc())
        .first()
    )
    if recent is None:
        return {"resendAvailableAt": None}
    created = recent.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    available_at = created + EMAIL_VERIFY_RESEND_COOLDOWN
    if available_at <= datetime.now(timezone.utc):
        return {"resendAvailableAt": None}
    return {"resendAvailableAt": available_at.isoformat()}


@router.post("/send-verification", status_code=status.HTTP_204_NO_CONTENT)
def send_verification(
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Response:
    """Send (or resend) the verification email for the current user.

    Rate-limited to one email per hour per user — returns 429 with a
    Retry-After header if a token was issued recently. No-op (returns
    204) if the user is already verified.
    """
    if current.email_verified:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # Most recently issued token for this user.
    recent = (
        db.query(EmailVerificationToken)
        .filter(EmailVerificationToken.user_id == current.id)
        .order_by(EmailVerificationToken.created_at.desc())
        .first()
    )
    if recent is not None:
        created = recent.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        elapsed = datetime.now(timezone.utc) - created
        if elapsed < EMAIL_VERIFY_RESEND_COOLDOWN:
            wait = (EMAIL_VERIFY_RESEND_COOLDOWN - elapsed).total_seconds()
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="A verification email was sent recently. Try again in an hour.",
                headers={"Retry-After": str(int(wait))},
            )

    _issue_verification_token(db, current, request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> Response:
    # Sign out of EVERY account on this browser — the active login plus
    # any others in the account-switcher bundle — revoking each session
    # and clearing both cookies. (Per-account sign-out is the switcher's
    # own Remove action; this button is "log out of everything here".)
    tokens = set()
    if session_token:
        tokens.add(session_token)
    tokens.update(get_linked_tokens(request))
    for t in tokens:
        s = db.get(UserSession, t)
        if s:
            db.delete(s)
    if tokens:
        db.commit()
    clear_session_cookie(response)
    clear_linked_cookie(response)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/sessions")
def list_sessions(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> list[dict]:
    """List the current user's active sessions for the Settings panel.

    Each row is enough for the UI to render a labeled device line +
    a revoke button. The session whose cookie made this request is
    marked `current: true` so the UI can disable its revoke action -
    revoking your own session would just log you out, which the
    Logout button already does.
    """
    rows = (
        db.query(UserSession)
        .filter(UserSession.user_id == current.id)
        .order_by(UserSession.created_at.desc())
        .all()
    )
    return [
        {
            "token": s.token,
            "createdAt": s.created_at.isoformat() if s.created_at else None,
            "expiresAt": s.expires_at.isoformat() if s.expires_at else None,
            "userAgent": s.user_agent,
            "ipAddress": s.ip_address,
            "current": s.token == session_token,
        }
        for s in rows
    ]


@router.delete("/sessions/{token}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_session(
    token: str,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> Response:
    """Revoke a single session by its token. Refuses to revoke the
    request's own session - the Logout button is the path for that."""
    if token == session_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Use logout to end the current session.",
        )
    s = db.get(UserSession, token)
    # Treat missing/cross-user as success - the end-state the caller
    # asked for (that token gone) is true, and we don't want to leak
    # whether the token ever existed across users.
    if s and s.user_id == current.id:
        db.delete(s)
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/sessions", status_code=status.HTTP_204_NO_CONTENT)
def revoke_other_sessions(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> Response:
    """Sign out everywhere except the session making this request."""
    q = db.query(UserSession).filter(UserSession.user_id == current.id)
    if session_token:
        q = q.filter(UserSession.token != session_token)
    q.delete(synchronize_session=False)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------- Account switcher (multiple signed-in accounts) ----------


class _AccountRef(BaseModel):
    userId: str


def _account_entry(db: Session, session: UserSession, *, active: bool):
    u = db.get(User, session.user_id)
    if u is None:
        return None
    return {
        "userId": u.id,
        "username": u.username,
        "email": u.email,
        "tier": u.effective_tier,
        "active": active,
    }


@router.get("/accounts")
def list_accounts(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> list[dict]:
    """Accounts signed in on this browser, for the Settings switcher.

    The active account is whichever archive336_session points at; the rest
    are the aether_linked bundle. Expired / invalid / duplicate tokens
    are pruned and the bundle cookie rewritten, so the list self-heals.
    """
    active_token = request.cookies.get(SESSION_COOKIE_NAME)
    original = get_linked_tokens(request)
    out: list[dict] = []
    seen: set[str] = set()

    active_session = resolve_session(db, active_token)
    if active_session:
        e = _account_entry(db, active_session, active=True)
        if e:
            out.append(e)
            seen.add(e["userId"])

    kept: list[str] = []
    for t in original:
        s = resolve_session(db, t)
        if not s or s.user_id in seen:
            continue
        e = _account_entry(db, s, active=False)
        if e:
            out.append(e)
            seen.add(e["userId"])
            kept.append(t)

    if kept != original:
        set_linked_cookie(response, kept)
    return out


@router.post("/accounts/switch", response_model=UserOut)
def switch_account(
    payload: _AccountRef,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> User:
    """Make a linked account the active one, demoting the previously-
    active login into the bundle. Picks by user id — raw tokens never
    leave the server."""
    active_token = request.cookies.get(SESSION_COOKIE_NAME)
    linked = get_linked_tokens(request)

    target_token = None
    for t in linked:
        s = resolve_session(db, t)
        if s and s.user_id == payload.userId:
            target_token = t
            break

    if target_token is None:
        # Already the active account → no-op success.
        active_session = resolve_session(db, active_token)
        if active_session and active_session.user_id == payload.userId:
            u = db.get(User, payload.userId)
            if u:
                return u
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That account isn't signed in on this browser.",
        )

    new_linked = [t for t in linked if t != target_token]
    if resolve_session(db, active_token):
        new_linked.insert(0, active_token)

    set_session_cookie(response, target_token)
    set_linked_cookie(response, new_linked)

    u = db.get(User, payload.userId)
    if u is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Account missing"
        )
    return u


@router.post("/accounts/remove")
def remove_account(
    payload: _AccountRef,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict:
    """Sign one account out of the switcher: revoke its session + drop
    it from the bundle. If it's the active account, promote another from
    the bundle (or clear everything if it was the last). Returns the
    user id active afterwards, or null if none remain."""
    active_token = request.cookies.get(SESSION_COOKIE_NAME)
    linked = get_linked_tokens(request)
    active_session = resolve_session(db, active_token)

    # Removing the currently-active account.
    if active_session and active_session.user_id == payload.userId:
        db.delete(active_session)
        db.commit()
        promoted = None  # (token, user_id)
        rest: list[str] = []
        for t in linked:
            s = resolve_session(db, t)
            if s is None:
                continue
            if promoted is None:
                promoted = (t, s.user_id)
            else:
                rest.append(t)
        if promoted:
            set_session_cookie(response, promoted[0])
            set_linked_cookie(response, rest)
            return {"activeUserId": promoted[1]}
        clear_session_cookie(response)
        clear_linked_cookie(response)
        return {"activeUserId": None}

    # Removing a linked (non-active) account.
    target_token = None
    for t in linked:
        s = resolve_session(db, t)
        if s and s.user_id == payload.userId:
            target_token = t
            break
    if target_token:
        s = db.get(UserSession, target_token)
        if s:
            db.delete(s)
            db.commit()
    set_linked_cookie(response, [t for t in linked if t != target_token])
    return {"activeUserId": active_session.user_id if active_session else None}


@router.get("/me", response_model=UserOut)
def me(current: User = Depends(get_current_user)) -> User:
    return current


@router.get("/ui-prefs")
def get_ui_prefs(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return the user's account-tied UI prefs (sidebar collapsed +
    layout, …), or {} if none. Frontend owns the schema. Fetched on
    login so the sidebar state follows the user across devices."""
    row = db.get(UserUiPrefs, current.id)
    if row is None:
        return {}
    try:
        return json.loads(row.prefs_json)
    except json.JSONDecodeError:
        return {}


@router.put("/ui-prefs", status_code=status.HTTP_204_NO_CONTENT)
def put_ui_prefs(
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Response:
    """Upsert the user's UI prefs. Frontend owns the schema."""
    blob = json.dumps(payload)
    row = db.get(UserUiPrefs, current.id)
    if row is None:
        row = UserUiPrefs(user_id=current.id, prefs_json=blob)
        db.add(row)
    else:
        row.prefs_json = blob
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/me", response_model=UserOut)
def update_profile(
    payload: UpdateProfileRequest,
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> User:
    """Update any subset of {username, email, password} in one request.

    The current password is always required as the gate — even for a
    username-only change, since it's the same dialog and we want one
    consistent rule. Each provided field is validated and applied;
    unspecified fields are left untouched. The whole update is
    atomic: if any check fails (wrong password, dupe, etc.) nothing
    changes.

    We do NOT invalidate other sessions on a password change here —
    the user is authenticated and presumed in control of their other
    devices. The forgot-password flow handles the
    presumed-compromised case by wiping all sessions.
    """
    if not verify_password(payload.current_password, current.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Wrong password.",
        )

    # Username
    if payload.new_username is not None:
        new_username = payload.new_username.strip()
        if new_username and new_username != current.username:
            taken = (
                db.query(User)
                .filter(User.username == new_username, User.id != current.id)
                .first()
            )
            if taken is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Username already taken.",
                )
            current.username = new_username

    # Email
    email_changed = False
    if payload.new_email is not None:
        new_email = payload.new_email.strip().lower()
        if new_email and new_email != current.email.lower():
            taken = (
                db.query(User)
                .filter(User.email == new_email, User.id != current.id)
                .first()
            )
            if taken is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="An account already exists for that email.",
                )
            current.email = new_email
            # A new address is unverified until proven — otherwise the
            # "verified" badge would vouch for an address we never confirmed.
            current.email_verified = False
            email_changed = True

    # Password
    if payload.new_password:
        current.password_hash = hash_password(payload.new_password)
        log.info("user %s changed password (via profile update)", current.id)

    db.commit()

    # Fire a fresh verification email for the new address (best-effort;
    # _issue_verification_token logs-and-swallows send failures).
    if email_changed:
        _issue_verification_token(db, current, request)

    db.refresh(current)
    return current


# ---------- Account export + deletion ----------


def _r2_keys_for_user(db: Session, user_id: str) -> List[str]:
    """Collect every R2 object key tied to this user so we can hand
    them to r2.delete_keys on account deletion. Includes:
      - thumbnails (UserChannelVideo.thumbnail_r2_key)
      - channel avatars (UserChannel.avatar_r2_key)
      - archived video files + their caption sidecars, in either the
        legacy `videos/...` or current `users/{uid}/videos/...` layout
    """
    keys: List[str] = []

    for v in (
        db.query(UserChannelVideo)
        .filter(UserChannelVideo.user_id == user_id)
        .all()
    ):
        if v.thumbnail_r2_key:
            keys.append(v.thumbnail_r2_key)
        try:
            data = json.loads(v.data_json)
        except json.JSONDecodeError:
            continue
        local_path = (data.get("localPath") or "").strip()
        # Accept BOTH key layouts, and pick up captions while we are here.
        #
        # This used to test only `videos/`, which stopped matching anything
        # the day uploads moved to `users/{uid}/videos/...`. The effect was
        # that deleting an account deleted the thumbnails and avatars and
        # left every mp4 and every .vtt caption sitting in the bucket
        # forever - the user's data survived the deletion they asked for,
        # and we kept paying to store it. The Video rows kept their
        # bytes_stored too, which is exactly the state
        # scripts/backfill_phantom_video_bytes.py exists to clean up: the
        # remediation was written while the thing generating the mess was
        # still running.
        #
        # storage_ledger.keys_from_video_data is the one enumeration that
        # already handles both layouts plus captions - the same helper the
        # channel purge was fixed to delegate to. Three hand-rolled copies
        # of this logic is how the drift happened; there are now two, and
        # this is the second one to be retired.
        keys.extend(storage_ledger.keys_from_video_data(data))

    for c in (
        db.query(UserChannel).filter(UserChannel.user_id == user_id).all()
    ):
        if c.avatar_r2_key:
            keys.append(c.avatar_r2_key)

    return keys


# In-memory rate-limit state for /me/export. Keyed by user_id, value
# is the epoch timestamp of the most recent export. Single uvicorn
# worker so this is sufficient; switch to a Redis-backed store when
# we go multi-process or multi-host.
_EXPORT_RATE_LIMIT_STATE: Dict[str, float] = {}
_EXPORT_RATE_LIMIT_LOCK = Lock()
_EXPORT_MIN_INTERVAL_SECONDS = 3600  # one export per user per hour


def _check_export_rate_limit(user_id: str) -> int:
    """Returns seconds-remaining if rate-limited, else 0 (allowed).

    Records the request as 'happened' iff allowed. Caller raises
    HTTPException(429) with Retry-After set to the returned value.
    """
    now = time.time()
    with _EXPORT_RATE_LIMIT_LOCK:
        last = _EXPORT_RATE_LIMIT_STATE.get(user_id)
        if last is not None and now - last < _EXPORT_MIN_INTERVAL_SECONDS:
            return int(_EXPORT_MIN_INTERVAL_SECONDS - (now - last))
        _EXPORT_RATE_LIMIT_STATE[user_id] = now
        return 0


@router.get("/me/export")
def export_my_data(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> StreamingResponse:
    """Stream a JSON-Lines (NDJSON) dump of the user's *personal* data
    plus a manifest of their archived files.

    Scope follows GDPR Art. 15 / 20 + CCPA: data ABOUT the user
    (account info, settings, connected accounts, billing) plus pointers
    to data they CREATED in the service (their channel/video list and
    R2 download URLs). Specifically NOT included: scraped YouTube
    metadata about the videos themselves (titles, descriptions,
    tags, view counts) - that's YouTube's data about YouTube videos,
    re-fetchable from YouTube any time, and stuffing it into the
    export bloats the file by orders of magnitude for no compliance
    benefit.

    Sensitive fields (password hash, encrypted OAuth tokens, raw
    session tokens, hashed one-time tokens) are also omitted.

    Why NDJSON instead of one big JSON object: a power user with
    10k videos produces a ~6 MB payload AND triggers ~10k synchronous
    R2 presign calls. The old version loaded everything into Python
    lists before returning; this version yields line-by-line so
    server memory stays flat and presigns interleave naturally.

    Rate-limited to one export per user per hour. The endpoint is
    expensive (DB walk + R2 ops) and trivially abusable to drain
    Hetzner bandwidth allowance.

    Schema: NDJSON, one JSON object per line. First line is
    ``{"type": "header", "schemaVersion": 3, ...}``; subsequent lines
    are ``{"type": "profile"|"googleConnection"|"channel"|"video"|
    "usageRecord"|"r2File"|"youTubeSettings", "data": {...}}``. A
    final ``{"type": "trailer", "ok": true}`` confirms the stream
    completed (truncation detection).
    """
    wait = _check_export_rate_limit(current.id)
    if wait > 0:
        raise HTTPException(
            status_code=429,
            detail=f"Already exported recently. Try again in {wait} seconds.",
            headers={"Retry-After": str(wait)},
        )

    user_id = current.id

    def _generate():
        # Header — schema version + timestamp.
        yield json.dumps(
            {
                "type": "header",
                "schemaVersion": 3,
                "exportedAt": datetime.now(timezone.utc).isoformat(),
            }
        ) + "\n"

        # Profile (single row, top-level).
        yield json.dumps(
            {
                "type": "profile",
                "data": {
                    "id": current.id,
                    "username": current.username,
                    "email": current.email,
                    "emailVerified": current.email_verified,
                    "isAdmin": current.is_admin,
                    "paymentStatus": current.payment_status,
                    "stripeCustomerId": current.stripe_customer_id,
                    "createdAt": (
                        current.created_at.isoformat() if current.created_at else None
                    ),
                    "updatedAt": (
                        current.updated_at.isoformat() if current.updated_at else None
                    ),
                },
            }
        ) + "\n"

        # Google connections — usually 1-3 rows, no need to stream
        # but using the same shape keeps the consumer simple.
        for c in (
            db.query(UserGoogleConnection)
            .filter(UserGoogleConnection.user_id == user_id)
            .yield_per(50)
        ):
            yield json.dumps(
                {
                    "type": "googleConnection",
                    "data": {
                        "googleUserId": c.google_user_id,
                        "googleEmail": c.google_email,
                        "youtubeChannelId": c.youtube_channel_id,
                        "youtubeChannelTitle": c.youtube_channel_title,
                        "scopes": c.scopes,
                        "connectedAt": (
                            c.connected_at.isoformat() if c.connected_at else None
                        ),
                    },
                }
            ) + "\n"

        # YouTube settings — single row.
        yt_row = (
            db.query(UserYouTubeSettings)
            .filter(UserYouTubeSettings.user_id == user_id)
            .first()
        )
        if yt_row is not None:
            try:
                yt_data = json.loads(yt_row.settings_json)
            except json.JSONDecodeError:
                yt_data = None
            yield json.dumps({"type": "youTubeSettings", "data": yt_data}) + "\n"

        # Channels — identifying fields only.
        for ch in (
            db.query(UserChannel)
            .filter(UserChannel.user_id == user_id)
            .yield_per(100)
        ):
            try:
                data = json.loads(ch.data_json)
            except json.JSONDecodeError:
                data = {}
            yield json.dumps(
                {
                    "type": "channel",
                    "data": {
                        "channelId": ch.channel_id,
                        "name": data.get("name") or "",
                        "handle": data.get("handle") or "",
                        "googleUserId": ch.google_user_id,
                        "addedAt": ch.added_at.isoformat() if ch.added_at else None,
                    },
                }
            ) + "\n"

        # Videos — status + size pointers only. yield_per(500) keeps
        # the SQLAlchemy result-set memory bounded as we walk what
        # could be tens of thousands of rows for a power user.
        for v in (
            db.query(UserChannelVideo)
            .filter(UserChannelVideo.user_id == user_id)
            .yield_per(500)
        ):
            try:
                vdata = json.loads(v.data_json)
            except json.JSONDecodeError:
                vdata = {}
            yield json.dumps(
                {
                    "type": "video",
                    "data": {
                        "channelId": v.channel_id,
                        "videoId": v.video_id,
                        "status": vdata.get("status"),
                        "fileSizeBytes": vdata.get("fileSizeBytes"),
                        "thumbnailSizeBytes": v.thumbnail_size_bytes,
                        "archivedAt": vdata.get("archivedAt"),
                    },
                }
            ) + "\n"

        # Usage records.
        for u in (
            db.query(UsageRecord)
            .filter(UsageRecord.user_id == user_id)
            .yield_per(500)
        ):
            yield json.dumps(
                {
                    "type": "usageRecord",
                    "data": {
                        "id": u.id,
                        "bytesStored": u.bytes_stored,
                        "billed": u.billed,
                        "billedAt": u.billed_at.isoformat() if u.billed_at else None,
                        "createdAt": (
                            u.created_at.isoformat() if u.created_at else None
                        ),
                    },
                }
            ) + "\n"

        # R2 manifest — the expensive part. Each key triggers one
        # presign call. Yielding between presigns means memory stays
        # flat AND the user sees the file start to flow on the wire
        # rather than waiting for all presigns to complete.
        for k in _r2_keys_for_user(db, user_id):
            try:
                url = r2.presign_get(k, expires_in=3600, subject=user_id)
            except Exception:  # noqa: BLE001
                url = None
            yield json.dumps(
                {"type": "r2File", "data": {"key": k, "downloadUrl": url}}
            ) + "\n"

        # Trailer — lets the consumer detect a truncated download
        # (if the stream ends without this line, something went wrong).
        yield json.dumps({"type": "trailer", "ok": True}) + "\n"

    return StreamingResponse(
        _generate(),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": (
                'attachment; filename="aether-archive-export.ndjson"'
            ),
            # Disable proxy buffering so chunks land at the client as
            # they're produced (matters behind Cloudflare + Caddy).
            "X-Accel-Buffering": "no",
        },
    )


def _outstanding_breakdown(db: Session, user: User) -> Dict[str, Any]:
    """Compute the dollar breakdown for what the user would owe at
    deletion time. Returns storage_usd, fee_usd, total_usd, has_card.
    """
    unbilled = (
        db.query(UsageRecord)
        .filter(
            UsageRecord.user_id == user.id,
            UsageRecord.billed.is_(False),
        )
        .all()
    )
    gb_days = sum(billing_lib.bytes_to_gb(r.bytes_stored) for r in unbilled)
    usd = billing_lib.gb_days_to_usd(gb_days)
    breakdown = billing_lib.compute_final_charge(usd)
    has_card = bool(
        user.stripe_customer_id
        and billing_lib.has_any_payment_method(user.stripe_customer_id)
    )
    breakdown["hasCard"] = has_card
    return {
        "storageUsd": breakdown["storage_usd"],
        "feeUsd": breakdown["fee_usd"],
        "totalUsd": breakdown["total_usd"],
        "hasCard": has_card,
    }


@router.get("/me/outstanding-charge")
def my_outstanding_charge(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Show the user how much we'd charge if they deleted right now.
    Powers the breakdown shown in the delete dialog so they can see
    + accept the amount before we email the confirmation link.
    """
    return _outstanding_breakdown(db, current)




def _build_export_payload(db: Session, user: User) -> Dict[str, Any]:
    """Same shape as GET /me/export. Pulled out so the post-delete
    email attachment can use the same payload without duplicating
    the assembly logic.
    """
    profile = {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "emailVerified": user.email_verified,
        "isAdmin": user.is_admin,
        "paymentStatus": user.payment_status,
        "stripeCustomerId": user.stripe_customer_id,
        "createdAt": user.created_at.isoformat() if user.created_at else None,
        "updatedAt": user.updated_at.isoformat() if user.updated_at else None,
    }
    google_connections = [
        {
            "googleUserId": c.google_user_id,
            "googleEmail": c.google_email,
            "youtubeChannelId": c.youtube_channel_id,
            "youtubeChannelTitle": c.youtube_channel_title,
            "scopes": c.scopes,
            "connectedAt": c.connected_at.isoformat() if c.connected_at else None,
        }
        for c in (
            db.query(UserGoogleConnection)
            .filter(UserGoogleConnection.user_id == user.id)
            .all()
        )
    ]
    channels: List[Dict[str, Any]] = []
    for ch in (
        db.query(UserChannel)
        .filter(UserChannel.user_id == user.id)
        .all()
    ):
        try:
            data = json.loads(ch.data_json)
        except json.JSONDecodeError:
            data = {}
        channels.append(
            {
                "channelId": ch.channel_id,
                "name": data.get("name") or "",
                "handle": data.get("handle") or "",
                "googleUserId": ch.google_user_id,
                "addedAt": ch.added_at.isoformat() if ch.added_at else None,
            }
        )
    videos: List[Dict[str, Any]] = []
    for v in (
        db.query(UserChannelVideo)
        .filter(UserChannelVideo.user_id == user.id)
        .all()
    ):
        try:
            data = json.loads(v.data_json)
        except json.JSONDecodeError:
            data = {}
        videos.append(
            {
                "channelId": v.channel_id,
                "videoId": v.video_id,
                "status": data.get("status"),
                "fileSizeBytes": data.get("fileSizeBytes"),
                "thumbnailSizeBytes": v.thumbnail_size_bytes,
                "archivedAt": data.get("archivedAt"),
            }
        )
    usage = [
        {
            "id": u.id,
            "bytesStored": u.bytes_stored,
            "billed": u.billed,
            "billedAt": u.billed_at.isoformat() if u.billed_at else None,
            "createdAt": u.created_at.isoformat() if u.created_at else None,
        }
        for u in (
            db.query(UsageRecord)
            .filter(UsageRecord.user_id == user.id)
            .all()
        )
    ]
    yt_settings_row = (
        db.query(UserYouTubeSettings)
        .filter(UserYouTubeSettings.user_id == user.id)
        .first()
    )
    yt_settings = None
    if yt_settings_row is not None:
        try:
            yt_settings = json.loads(yt_settings_row.settings_json)
        except json.JSONDecodeError:
            yt_settings = None
    return {
        "schemaVersion": 2,
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "profile": profile,
        "googleConnections": google_connections,
        "youTubeSettings": yt_settings,
        "channels": channels,
        "videos": videos,
        "usageRecords": usage,
    }


@router.post("/me/request-delete", status_code=status.HTTP_204_NO_CONTENT)
def request_account_deletion(
    payload: RequestAccountDeletionRequest,
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Response:
    """Step 1 of the delete flow (in-dialog). Re-auths the user,
    charges any outstanding storage synchronously, builds the export
    JSON if requested, mints a one-time deletion token, and sends a
    verification email containing the link to actually trigger the
    wipe. Returns 204 on success - the dialog flips to 'check your
    email.'

    The account is NOT deleted at this point. The email is the
    verification gate. If the user never clicks the link, the token
    expires (1h TTL) and the account stays alive (they paid for
    real storage they used, that part stands).

    Card decline / Stripe error here means no email is sent and the
    dialog can show an inline error - we don't want to email a
    'click to delete' link if we couldn't capture the money owed.
    """
    if not verify_password(payload.current_password, current.password_hash):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="That password is incorrect.",
        )

    # Charge any outstanding storage before touching the deletion
    # token or sending email - if the card declines we want the
    # dialog to surface that, not the email-link page.
    if current.stripe_customer_id:
        unbilled_records = (
            db.query(UsageRecord)
            .filter(
                UsageRecord.user_id == current.id,
                UsageRecord.billed.is_(False),
            )
            .order_by(UsageRecord.day)
            .all()
        )
        gb_days = sum(
            billing_lib.bytes_to_gb(r.bytes_stored) for r in unbilled_records
        )
        usd = billing_lib.gb_days_to_usd(gb_days)
        breakdown = billing_lib.compute_final_charge(usd)
        amount_cents = round(breakdown["total_usd"] * 100)
        if amount_cents > 0 and billing_lib.has_any_payment_method(
            current.stripe_customer_id
        ):
            period_start = (
                unbilled_records[0].day
                if unbilled_records
                else datetime.now(timezone.utc)
            )
            period_end = (
                unbilled_records[-1].day
                if unbilled_records
                else datetime.now(timezone.utc)
            )
            description = "Final storage charge — " + (
                billing_lib.storage_period_description(
                    period_start, period_end
                )
            )
            try:
                billing_lib.bill_outstanding_now(
                    customer_id=current.stripe_customer_id,
                    amount_cents=amount_cents,
                    description=description,
                    period_start=period_start,
                    period_end=period_end,
                )
            except stripe.error.CardError as e:
                log.warning(
                    "request-delete: card declined for user %s: %s",
                    current.id,
                    e.user_message or e,
                )
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail=(
                        f"Couldn't charge ${amount_cents / 100:.2f}. "
                        + (e.user_message or "Update your card and try again.")
                    ),
                )
            except Exception:
                log.exception(
                    "request-delete: bill_outstanding_now failed for user %s",
                    current.id,
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=(
                        "Couldn't reach the payment processor to charge "
                        f"${amount_cents / 100:.2f}."
                    ),
                )
            else:
                for r in unbilled_records:
                    r.billed = True
                db.commit()

    # Build export payload BEFORE the wipe so the data is still
    # around. Attached to the verification email if the user opted in.
    export_bytes: Optional[bytes] = None
    if payload.export_requested:
        try:
            export_bytes = json.dumps(
                _build_export_payload(db, current), indent=2
            ).encode("utf-8")
        except Exception:  # noqa: BLE001
            log.exception(
                "request-delete: export build failed for user %s; continuing",
                current.id,
            )

    # Mint the one-time token. Plaintext goes in the email URL; only
    # the SHA-256 hash hits the DB (same shape as PasswordResetToken).
    plaintext = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    db.add(
        AccountDeletionToken(
            token_hash=token_hash,
            user_id=current.id,
            charge_amount_cents=0,  # already charged; field kept for schema compat
            export_requested=payload.export_requested,
            expires_at=datetime.now(timezone.utc) + ACCOUNT_DELETION_TTL,
        )
    )
    db.commit()

    origin = _frontend_origin(request)
    confirm_url = (
        f"{origin.rstrip('/')}/confirm-delete?{urlencode({'token': plaintext})}"
    )

    try:
        email_lib.send_account_deletion_confirmation(
            to_email=current.email,
            confirm_url=confirm_url,
            export_json_bytes=export_bytes,
        )
        db.add(
            EmailSendLog(type="delete_confirmation", to_email=current.email)
        )
        db.commit()
    except Exception:
        log.exception(
            "request-delete: failed to send confirmation email for user %s",
            current.id,
        )
        # Don't surface the email failure - the user can request again
        # and the token they got will still work for an hour.

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/me/confirm-delete", status_code=status.HTTP_204_NO_CONTENT)
def confirm_account_deletion(
    payload: ConfirmAccountDeletionRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> Response:
    """Step 2 of the delete flow (email-link page). Validates the
    token, runs the actual wipe (Stripe customer, R2 keys, DB row),
    and clears the session cookie. No password required - the token
    IS the proof of identity + intent (the user might be on a
    different device than the one with their session).

    The card was already charged at /me/request-delete time, so by
    this point the only thing left is the actual deletion.
    """
    token_hash = hashlib.sha256(
        payload.token.encode("utf-8")
    ).hexdigest()
    row = db.get(AccountDeletionToken, token_hash)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This deletion link is invalid.",
        )
    if row.used_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This deletion link has already been used.",
        )
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This deletion link has expired. Start over from Settings.",
        )
    user = db.get(User, row.user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This deletion link is invalid.",
        )

    # Stripe cleanup
    if user.stripe_customer_id:
        try:
            billing_lib.delete_customer(user.stripe_customer_id)
        except Exception:  # noqa: BLE001
            log.exception(
                "confirm-delete: stripe customer delete failed for user %s",
                user.id,
            )

    # R2 cleanup. R2 first → ledger second per the design ordering rule
    # (over-charge briefly recoverable, under-charge isn't).
    keys = _r2_keys_for_user(db, user.id)
    if keys:
        try:
            r2.delete_keys(keys)
            # Flip deleted_at on every StorageObject for these keys.
            # If this DB write fails after R2 succeeds, reconciliation
            # catches the orphan-in-ledger within 24h.
            storage_ledger.mark_deleted(db, keys)
        except Exception:  # noqa: BLE001
            log.exception(
                "confirm-delete: R2 cleanup failed for user %s", user.id
            )
        # The shared `videos` table is channel-keyed, not user-keyed, so the
        # user delete below does NOT cascade its rows. Left alone they keep
        # this user's r2_key + bytes_stored after the objects are gone, and
        # billing sums bytes_stored with no storage join - phantom bytes that
        # overbill whoever else subscribes to the channel. Null them now so
        # no orphan is created; reconcile.py is the backstop for any missed.
        db.query(Video).filter(Video.r2_key.in_(keys)).update(
            {
                Video.bytes_stored: None,
                Video.r2_key: None,
                Video.synced_at: None,
            },
            synchronize_session=False,
        )

    # Mark token used (would cascade with the user delete anyway, but
    # explicit for the audit trail).
    row.used_at = datetime.now(timezone.utc)
    db.commit()

    user_id_for_log = user.id
    db.delete(user)
    db.commit()
    log.info("user %s deleted (via confirm-delete)", user_id_for_log)

    clear_session_cookie(response)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------- YouTube OAuth (Google sign-in for the user's own channel) ----------
#
# Flow:
#   1. Frontend → GET /api/auth/youtube/start
#      We mint a CSRF state token, set it in an HttpOnly cookie, redirect
#      the browser to Google's consent screen.
#   2. Google → GET /api/auth/youtube/callback?code=…&state=…
#      We verify the state matches the cookie, exchange the code for
#      tokens, fetch the user's profile + their YouTube channel, and
#      persist everything (tokens encrypted at rest). Then redirect the
#      browser back to the frontend with a success flag.
#   3. Frontend → GET /api/auth/youtube/status
#      Returns whether the current user is connected and which channel.

OAUTH_STATE_COOKIE = "aether_oauth_state"
OAUTH_USER_COOKIE = "aether_oauth_user"  # tied to the ARCHIVE336 user starting the flow
OAUTH_VERIFIER_COOKIE = "aether_oauth_verifier"  # PKCE code_verifier


def _frontend_origin(request: Request) -> str:
    """Where to redirect the browser after the OAuth callback succeeds.

    In production this is archive336.com. In local dev the frontend
    runs on localhost:8787 and proxies /api → 8000, so we redirect there.
    """
    explicit = os.environ.get("ARCHIVE336_FRONTEND_ORIGIN")
    if explicit:
        return explicit.rstrip("/")
    # Infer from the redirect URI scheme/host
    redirect = os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "")
    if "localhost" in redirect:
        return "http://localhost:8787"
    return "https://archive336.com"


def _is_secure_origin() -> bool:
    redirect = os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "")
    return redirect.startswith("https://")


@router.get("/youtube/start")
def youtube_oauth_start(
    current: User = Depends(get_current_user),
) -> RedirectResponse:
    """Kick off the Google OAuth flow. Returns a 307 to Google's consent screen."""
    # Tier gate. Connecting a Google account is only meaningful for
    # Creator+ users (their channel jobs run on the worker pool and
    # need OAuth tokens to authenticate as them) and internal tiers
    # (dev/admin/etc. for testing). Basic users sync via their own
    # worker app's embedded webview, so they don't get to OAuth here.
    # Frontend already hides the Settings section; this is the
    # belt-and-braces server-side gate.
    if not tiers.can_connect_external_accounts(current):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Connecting a Google account requires Creator tier or higher.",
        )
    state = secrets.token_urlsafe(32)
    auth_url, code_verifier = google_oauth.authorization_url(state=state)

    redirect = RedirectResponse(url=auth_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    # Three short-lived cookies:
    #   state    — CSRF token, must round-trip Google
    #   user     — which ARCHIVE336 user is connecting (session cookie isn't
    #              always sent back on cross-site redirect)
    #   verifier — PKCE code_verifier, must match the challenge in the
    #              auth URL so Google accepts the code exchange
    secure = _is_secure_origin()
    for name, value in (
        (OAUTH_STATE_COOKIE, state),
        (OAUTH_USER_COOKIE, current.id),
        (OAUTH_VERIFIER_COOKIE, code_verifier),
    ):
        redirect.set_cookie(
            name,
            value,
            max_age=600,
            httponly=True,
            secure=secure,
            samesite="lax",
            path="/api/auth/youtube",
        )
    return redirect


@router.get("/youtube/callback")
def youtube_oauth_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Google redirects here after the user consents (or denies)."""
    frontend = _frontend_origin(request)

    def _redirect_with(params: dict) -> RedirectResponse:
        target = f"{frontend}/settings?{urlencode(params)}"
        resp = RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)
        # Clear OAuth scratch cookies regardless of outcome
        for name in (OAUTH_STATE_COOKIE, OAUTH_USER_COOKIE, OAUTH_VERIFIER_COOKIE):
            resp.delete_cookie(name, path="/api/auth/youtube")
        return resp

    if error:
        return _redirect_with({"yt_connect": "error", "reason": error})

    cookie_state = request.cookies.get(OAUTH_STATE_COOKIE)
    user_id = request.cookies.get(OAUTH_USER_COOKIE)
    code_verifier = request.cookies.get(OAUTH_VERIFIER_COOKIE)
    if not (code and state and cookie_state and user_id and code_verifier):
        return _redirect_with({"yt_connect": "error", "reason": "missing_params"})
    if not secrets.compare_digest(state, cookie_state):
        return _redirect_with({"yt_connect": "error", "reason": "state_mismatch"})

    user = db.get(User, user_id)
    if user is None:
        return _redirect_with({"yt_connect": "error", "reason": "unknown_user"})

    try:
        creds = google_oauth.exchange_code(code, code_verifier=code_verifier)
    except Exception:  # noqa: BLE001
        log.exception("OAuth code exchange failed for user %s", user_id)
        return _redirect_with({"yt_connect": "error", "reason": "exchange_failed"})

    try:
        userinfo = google_oauth.fetch_userinfo(creds)
    except Exception:  # noqa: BLE001
        log.exception("OAuth userinfo fetch failed for user %s", user_id)
        return _redirect_with({"yt_connect": "error", "reason": "userinfo_failed"})

    try:
        channel = google_oauth.fetch_my_channel(creds)
    except Exception:  # noqa: BLE001
        log.exception("YouTube channel fetch failed for user %s", user_id)
        # Not fatal — the user might not have a channel yet.
        channel = None

    google_user_id = userinfo.get("id") or userinfo.get("sub") or ""
    google_email = userinfo.get("email") or ""

    expires = creds.expiry
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)

    # Composite PK now: (user_id, google_user_id). Re-connecting the
    # same Google account refreshes the existing row's tokens; a
    # different Google account inserts a second row alongside it.
    row = db.get(UserGoogleConnection, (user.id, google_user_id))
    if row is None:
        row = UserGoogleConnection(
            user_id=user.id,
            google_user_id=google_user_id,
            google_email=google_email,
            access_token_enc=encryption.encrypt(creds.token),
            refresh_token_enc=encryption.encrypt(creds.refresh_token or ""),
            access_token_expires_at=expires,
            scopes=" ".join(creds.scopes or []),
        )
        db.add(row)
    else:
        row.google_email = google_email
        row.access_token_enc = encryption.encrypt(creds.token)
        if creds.refresh_token:
            row.refresh_token_enc = encryption.encrypt(creds.refresh_token)
        row.access_token_expires_at = expires
        row.scopes = " ".join(creds.scopes or [])
        # Re-auth clears any prior disconnect. If we don't reset these
        # the row stays flagged after a successful reconnect and
        # _load_user_credentials will short-circuit it forever.
        row.disconnected_at = None
        row.disconnect_reason = None

    if channel:
        row.youtube_channel_id = channel["id"]
        row.youtube_channel_title = (channel.get("snippet") or {}).get("title")

    db.commit()
    return _redirect_with({"yt_connect": "ok"})


@router.get("/youtube/status")
def youtube_oauth_status(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> dict:
    """Return the user's YouTube connection state.

    The UI uses this to render the list of connected YouTube accounts
    on the settings page. We deliberately don't return any token
    material — just the public-facing identity.

    Multi-account: returns one entry per connected Google account,
    ordered oldest-first so the original primary account stays at
    the top after additional ones are connected.
    """
    # Tier split: Creator+ / internal connect via OAuth (the worker pool syncs
    # as them), so their connections are UserGoogleConnection rows. Basic users
    # run their own worker app and connect YouTube there (cookies), so mirror
    # what the app reported instead. Core users don't sync at all.
    if not tiers.can_connect_external_accounts(current):
        if tiers.effective_tier(current) == "basic":
            wc = db.get(WorkerYoutubeConnection, current.id)
            if wc is not None and wc.connected:
                # The worker reports connection state but not the channel name,
                # so fill it in from the user's owned channel. wc.channel_title
                # wins if the worker ever starts sending it directly.
                owned_title = (
                    db.query(Channel.title)
                    .join(
                        ChannelOwnership,
                        ChannelOwnership.channel_id == Channel.id,
                    )
                    .filter(
                        ChannelOwnership.user_id == current.id,
                        ChannelOwnership.revoked_at.is_(None),
                    )
                    .order_by(ChannelOwnership.authenticated_at.asc())
                    .scalar()
                )
                return {
                    "connections": [
                        {
                            "googleUserId": "worker",
                            "googleEmail": None,
                            "youtubeChannelId": None,
                            "youtubeChannelTitle": (
                                wc.channel_title
                                or owned_title
                                or "Your YouTube account"
                            ),
                            "connectedAt": (
                                wc.reported_at.isoformat() if wc.reported_at else None
                            ),
                            "imported": False,
                            "disconnected": False,
                            "disconnectedAt": None,
                            "source": "worker",
                        }
                    ]
                }
        return {"connections": []}

    rows = (
        db.query(UserGoogleConnection)
        .filter(UserGoogleConnection.user_id == current.id)
        .order_by(UserGoogleConnection.connected_at.asc())
        .all()
    )

    # Pull every imported channel for this user in one query, then
    # answer "is this connection's youtube_channel_id imported?" from
    # an in-memory set. Beats a per-row .get() round trip.
    #
    # Exclude soft-deleted rows - a user who disconnects (which soft-
    # deletes their imported channels) then reconnects should see the
    # "Import channel" CTA again so they can pull the data back into
    # an active state. Without this filter the button stays hidden and
    # there's no path out of "removed but still connected".
    imported_channel_ids = {
        cid
        for (cid,) in (
            db.query(UserChannel.channel_id)
            .filter(
                UserChannel.user_id == current.id,
                UserChannel.removed_at.is_(None),
            )
            .all()
        )
    }

    connections = [
        {
            "googleUserId": r.google_user_id,
            "googleEmail": r.google_email,
            "youtubeChannelId": r.youtube_channel_id,
            "youtubeChannelTitle": r.youtube_channel_title,
            "connectedAt": r.connected_at.isoformat() if r.connected_at else None,
            "imported": (
                r.youtube_channel_id is not None
                and r.youtube_channel_id in imported_channel_ids
            ),
            # When Google rejects a token refresh (user revoked us, etc.),
            # the connection is flagged here. Frontend swaps the
            # disconnect button for a reconnect CTA in that case.
            "disconnected": r.disconnected_at is not None,
            "disconnectedAt": (
                r.disconnected_at.isoformat() if r.disconnected_at else None
            ),
        }
        for r in rows
    ]
    return {"connections": connections}


@router.post(
    "/youtube/disconnect/{google_user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def youtube_oauth_disconnect_one(
    google_user_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Response:
    """Drop the stored tokens for a single connected Google account,
    revoke the grant at Google, AND soft-delete every channel imported
    via that account.

    Mental model: 'disconnect' from the user's POV means 'I'm done
    with this account.' If we left the channels behind they'd just
    sit in the dashboard hanging off a connection that doesn't exist
    anymore - confusing and a cost leak. Soft-delete sends them to
    'Recently removed' with the standard 30-day grace, billing
    pauses, and the user can restore inside the window if they
    change their mind.

    Best-effort revoke at Google's end via the refresh token: if it
    succeeds, the grant disappears from the user's connected-apps
    list and their next reconnect goes through a fresh consent
    dialog instead of being silently re-granted. Failures (network,
    invalid_token) are logged but don't block the local cleanup -
    local-side state matters more for correctness. No 404 on
    missing connection since stale frontend caching shouldn't
    error out the user.
    """
    now = datetime.now(timezone.utc)

    row = db.get(UserGoogleConnection, (current.id, google_user_id))
    if row is not None:
        try:
            refresh_token = encryption.decrypt(row.refresh_token_enc)
            google_oauth.revoke_token(refresh_token)
        except Exception as e:
            log.warning(
                "google revoke failed for user %s gid %s: %s",
                current.id,
                google_user_id,
                e,
            )

    # Soft-delete any channels linked to this Google account. We use
    # the same removed_at semantics as the manual remove-channel
    # flow: the daily purge cron picks them up after 30 days.
    db.query(UserChannel).filter(
        UserChannel.user_id == current.id,
        UserChannel.google_user_id == google_user_id,
        UserChannel.removed_at.is_(None),
    ).update({"removed_at": now}, synchronize_session=False)

    if row is not None:
        db.delete(row)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/youtube/disconnect", status_code=status.HTTP_204_NO_CONTENT)
def youtube_oauth_disconnect_all(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> Response:
    """Drop *every* Google connection for the current user and revoke
    each one at Google.

    Kept around for completeness (e.g. account deletion) but the UI
    uses the per-account variant above. No frontend currently calls
    this endpoint after Step D. Each revoke is best-effort - one
    failing token doesn't block the rest.
    """
    rows = (
        db.query(UserGoogleConnection)
        .filter(UserGoogleConnection.user_id == current.id)
        .all()
    )
    for row in rows:
        try:
            refresh_token = encryption.decrypt(row.refresh_token_enc)
            google_oauth.revoke_token(refresh_token)
        except Exception as e:
            log.warning(
                "google revoke failed for user %s gid %s: %s",
                current.id,
                row.google_user_id,
                e,
            )
        db.delete(row)
    if rows:
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
