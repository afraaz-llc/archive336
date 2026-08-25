"""Shared helper to load + refresh + persist Google OAuth credentials.

Moved out of routes/youtube.py so cron scripts (rescan_metadata,
rescan_comments) can use the same code path that the API routes use.
Handles the full lifecycle: decrypt persisted tokens, attempt a
refresh, mark the connection disconnected on unrecoverable failure,
send the one-time notification email.

Returns None for any reason the connection isn't usable (no row,
already-marked disconnected, decryption failed, refresh failed). The
caller treats None the same as "no connection".
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app import email as email_lib, encryption, google_oauth
from app.models import User, UserChannel, UserGoogleConnection


log = logging.getLogger(__name__)


def load_user_credentials(
    db: Session,
    user_id: str,
    google_user_id: Optional[str] = None,
) -> Optional[google_oauth.Credentials]:
    """Load + decrypt + (lazily) refresh the user's Google credentials.

    If ``google_user_id`` is given, uses that specific connection.
    Otherwise falls back to the user's first (oldest) connection.

    Side effects:
      - If the access token gets refreshed, the new token + expiry is
        persisted back to the DB.
      - If refresh fails with OAuthDisconnected, the row is marked
        disconnected and a one-time notification email is sent.

    Returns None on any failure path; the caller cannot distinguish
    between "never connected" / "disconnected" / "refresh failed" from
    the return value (logs disambiguate).
    """
    if google_user_id is not None:
        row = db.get(UserGoogleConnection, (user_id, google_user_id))
    else:
        row = (
            db.query(UserGoogleConnection)
            .filter(UserGoogleConnection.user_id == user_id)
            .order_by(UserGoogleConnection.connected_at.asc())
            .first()
        )
    if row is None:
        return None
    # Already-disconnected connections short-circuit; calling refresh
    # again would just fail with the same reason.
    if row.disconnected_at is not None:
        return None
    try:
        access = encryption.decrypt(row.access_token_enc)
        refresh = encryption.decrypt(row.refresh_token_enc)
    except Exception:  # noqa: BLE001
        return None

    creds = google_oauth.credentials_from_stored(
        access_token=access,
        refresh_token=refresh,
        expires_at=row.access_token_expires_at,
        scopes=row.scopes,
    )
    try:
        creds, refreshed = google_oauth.refresh_if_needed(creds)
    except google_oauth.OAuthDisconnected as e:
        row.disconnected_at = datetime.now(timezone.utc)
        row.disconnect_reason = e.reason
        db.commit()
        log.warning(
            "OAuth disconnected for user=%s google_user=%s reason=%s",
            user_id, row.google_user_id, e.reason,
        )
        user = db.get(User, user_id)
        if user is not None:
            # Respect the user's notification preference — this used to fire
            # unconditionally, which made the toggle a lie in both directions.
            # Account-level event, so it reads the account-level setting
            # (defaults on: losing sync is something you want to hear about).
            from app import notify as notify_lib  # noqa: WPS433

            if notify_lib.user_flag(db, user_id, "notifyOauthDisconnected"):
                # Deep-link at the channel this connection actually
                # authenticated, with its settings panel open (?panel=settings
                # is what ChannelDetail reads) - that panel is where
                # reconnecting happens. One connection can back several
                # channels; with anything other than exactly one there is no
                # single page to send them to, so fall back to the list.
                linked = [
                    cid
                    for (cid,) in db.query(UserChannel.channel_id).filter(
                        UserChannel.user_id == user_id,
                        UserChannel.google_user_id == row.google_user_id,
                        UserChannel.removed_at.is_(None),
                    )
                ]
                if len(linked) == 1:
                    target = (
                        "https://archive336.com/youtube/channel/"
                        f"{linked[0]}?panel=settings"
                    )
                else:
                    target = "https://archive336.com/youtube"
                try:
                    email_lib.send_oauth_disconnected(user.email, target)
                except Exception:  # noqa: BLE001
                    log.exception(
                        "failed to send oauth-disconnected email to %s",
                        user.email,
                    )
        return None

    if refreshed:
        # Persist the new access token + expiry so we don't refresh on
        # every call. Also re-encrypt the refresh_token if Google
        # rotated it (rare but possible per OAuth spec).
        try:
            row.access_token_enc = encryption.encrypt(creds.token)
            if creds.refresh_token and creds.refresh_token != refresh:
                row.refresh_token_enc = encryption.encrypt(creds.refresh_token)
            if creds.expiry:
                expiry = creds.expiry
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                row.access_token_expires_at = expiry
            db.commit()
        except Exception:  # noqa: BLE001
            log.exception("failed to persist refreshed access token")
            db.rollback()

    return creds
