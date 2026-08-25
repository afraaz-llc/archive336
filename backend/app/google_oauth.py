"""Google OAuth + YouTube Data API helpers.

Wraps ``google_auth_oauthlib`` for the auth-code flow we use to connect
a user's YouTube account, and ``googleapiclient`` for actually calling
the Data API once we have tokens.

Scopes:
- ``youtube.readonly``: list channels, videos, playlists, captions for
  the authenticated user — including private/unlisted/members.
- ``youtube.force-ssl``: required by commentThreads.list / comments.list
  even for read-only access. Despite the name, this scope is the
  documented minimum for reading comments via the Data API.
- ``openid``, ``email``, ``profile``: get the user's stable Google
  account identifier (sub) + email so we can de-dupe and show "Connected
  as x@y.com" in the UI.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple  # noqa: F401

import requests
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


class OAuthDisconnected(Exception):
    """The stored refresh token is no longer accepted by Google.

    Raised by refresh_if_needed when Google returns invalid_grant or any
    other unrecoverable error during the refresh exchange. Callers
    should mark the matching UserGoogleConnection row as disconnected
    instead of retrying.
    """

    def __init__(self, reason: str, original: Optional[Exception] = None):
        super().__init__(reason)
        self.reason = reason
        self.original = original


SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]


def _client_config() -> Dict[str, Any]:
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    redirect_uri = os.environ.get("GOOGLE_OAUTH_REDIRECT_URI")
    if not (client_id and client_secret and redirect_uri):
        raise RuntimeError(
            "Google OAuth env vars missing (GOOGLE_OAUTH_CLIENT_ID, "
            "GOOGLE_OAUTH_CLIENT_SECRET, GOOGLE_OAUTH_REDIRECT_URI)"
        )
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uris": [redirect_uri],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def _flow(redirect_uri: Optional[str] = None) -> Flow:
    config = _client_config()
    flow = Flow.from_client_config(config, scopes=SCOPES)
    flow.redirect_uri = redirect_uri or config["web"]["redirect_uris"][0]
    return flow


def authorization_url(
    state: str, redirect_uri: Optional[str] = None
) -> Tuple[str, str]:
    """Build the URL we redirect the user to to start the OAuth dance.

    Returns ``(auth_url, code_verifier)``. The verifier needs to be
    persisted (e.g. in an HttpOnly cookie) so the callback handler can
    pass it to :func:`exchange_code` — Google requires PKCE.

    ``state`` is a CSRF token we generated, opaque to Google. We get it
    back via the callback's query string and verify it matches what we
    stashed for this session.

    ``access_type=offline`` + ``prompt=consent`` ensures Google issues a
    refresh token. (Without prompt=consent, Google sometimes skips it on
    repeat consents.)

    ``prompt`` also includes ``select_account`` so the user always gets
    the account chooser — without it, Google auto-uses whichever Google
    account is currently signed into the browser, which makes adding a
    *second* ARCHIVE336-connected account impossible.
    """
    flow = _flow(redirect_uri=redirect_uri)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent select_account",
        state=state,
    )
    # google-auth-oauthlib auto-generates a verifier and stores it on the
    # flow; we lift it out so the caller can stash it in a cookie.
    return auth_url, flow.code_verifier


def exchange_code(
    code: str, code_verifier: str, redirect_uri: Optional[str] = None
) -> Credentials:
    """Trade the authorization code Google sent us for access + refresh tokens.

    ``code_verifier`` must be the same value we generated in
    :func:`authorization_url` and round-tripped through the cookie.
    """
    flow = _flow(redirect_uri=redirect_uri)
    flow.code_verifier = code_verifier
    flow.fetch_token(code=code)
    return flow.credentials


def revoke_token(token: str) -> None:
    """Revoke a Google OAuth token at Google's end.

    POSTs to ``https://oauth2.googleapis.com/revoke``. Accepts either an
    access or a refresh token; passing the refresh token revokes the
    entire authorization grant (and every access token derived from
    it). That's what we want on disconnect: without it, Google
    remembers the prior consent and silently re-grants on the user's
    next OAuth attempt — even with ``prompt=consent``, the consent
    screen becomes a one-click rubber stamp instead of a real
    permissions ask.

    Raises requests.HTTPError on non-2xx (including ``400 invalid_token``
    when the token is already revoked). Callers should treat both
    network errors and invalid_token as best-effort, since the local
    delete must succeed regardless of Google's state.
    """
    resp = requests.post(
        "https://oauth2.googleapis.com/revoke",
        data={"token": token},
        timeout=10,
    )
    resp.raise_for_status()


def credentials_from_stored(
    access_token: str,
    refresh_token: str,
    expires_at: datetime,
    scopes: str,
) -> Credentials:
    """Reconstruct a Credentials object from values we previously persisted."""
    config = _client_config()["web"]
    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri=config["token_uri"],
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        scopes=scopes.split(" ") if scopes else SCOPES,
    )
    # google-auth uses naive UTC datetimes for .expiry
    if expires_at.tzinfo is not None:
        creds.expiry = expires_at.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        creds.expiry = expires_at
    return creds


def refresh_if_needed(creds: Credentials) -> Tuple[Credentials, bool]:
    """If the access token is expired (or close to it), refresh it.

    Returns ``(creds, was_refreshed)``. Caller should re-persist tokens
    when ``was_refreshed`` is True.

    Raises ``OAuthDisconnected`` if Google rejects the refresh - most
    commonly because the user revoked our app in their Google account
    security settings, but also covers expired/rotated refresh tokens
    and revoked-via-password-change cases. The exception carries a
    short reason string from Google for logging/diagnostics.
    """
    if creds.valid and not creds.expired:
        return creds, False
    try:
        creds.refresh(GoogleRequest())
    except RefreshError as e:
        # google-auth's RefreshError pickles the body of Google's error
        # response into the first arg. Pull out the 'error' code if
        # present so we can show a stable reason; fall back to the
        # raw string when the shape doesn't match.
        reason = "invalid_grant"
        if e.args:
            payload = e.args[-1] if len(e.args) > 1 else e.args[0]
            if isinstance(payload, dict):
                reason = str(payload.get("error", reason))
            elif isinstance(payload, str) and payload:
                reason = payload[:80]
        raise OAuthDisconnected(reason, e) from e
    return creds, True


def fetch_userinfo(creds: Credentials) -> Dict[str, Any]:
    """Return the user's basic Google profile (sub, email, name)."""
    service = build("oauth2", "v2", credentials=creds, cache_discovery=False)
    return service.userinfo().get().execute()


def fetch_my_channel(creds: Credentials) -> Optional[Dict[str, Any]]:
    """Return the YouTube channel for the authenticated user, or None.

    A Google account can have zero channels (most do not have a YouTube
    channel until they create one). We treat that as "not connected" and
    surface a nicer message in the UI.
    """
    service = build("youtube", "v3", credentials=creds, cache_discovery=False)
    resp = service.channels().list(
        part="snippet,statistics,brandingSettings,status,contentDetails",
        mine=True,
    ).execute()
    items = resp.get("items") or []
    if not items:
        return None
    return items[0]


def fetch_all_video_ids(creds: Credentials, uploads_playlist_id: str) -> list:
    """Page through the uploads playlist and return every video ID.

    Each playlistItems.list call costs 1 quota unit and returns up to 50
    items; cheap even for large channels.
    """
    service = build("youtube", "v3", credentials=creds, cache_discovery=False)
    ids = []
    page_token: Optional[str] = None
    while True:
        resp = service.playlistItems().list(
            part="contentDetails",
            playlistId=uploads_playlist_id,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        for item in resp.get("items", []):
            vid = (item.get("contentDetails") or {}).get("videoId")
            if vid:
                ids.append(vid)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def fetch_video_details(creds: Credentials, video_ids: list) -> list:
    """Batched videos.list — up to 50 IDs per call. 1 quota unit per call.

    Returns the raw API items in the same order as input batches. Skipped
    videos (e.g. region-blocked, deleted between playlist fetch and this
    call) just don't appear in the output.
    """
    if not video_ids:
        return []
    service = build("youtube", "v3", credentials=creds, cache_discovery=False)
    out = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        resp = service.videos().list(
            part="snippet,contentDetails,statistics,status",
            id=",".join(batch),
            maxResults=50,
        ).execute()
        out.extend(resp.get("items", []))
    return out


def fetch_video_comments(
    creds: Credentials,
    video_id: str,
    order: str = "time",
) -> list:
    """Fetch every comment + every reply on a video, recursively.

    Walks YouTube's commentThreads endpoint (top-level threads, paged)
    and resolves each thread's replies (via threads inline or by paging
    `comments.list?parentId=`). Each row returned is normalized into a
    flat dict:

        {
          "id": "...",                      # globally-unique comment ID
          "parent_id": None | "...",        # None for top-level
          "author": "...",
          "author_channel_id": "..." | None,
          "text": "...",
          "like_count": int,
          "published_at": ISO-8601 string or None,
          "updated_at": ISO-8601 string or None,
          "is_edited": bool,
          "is_pinned": bool,
          "is_by_uploader": bool,
          "viewer_rating_like": bool,
        }

    Cost: 1 quota unit per page (100 items). A video with N top-level
    comments and R total replies costs ceil(N/100) + ceil(R/100) units
    in the common case (most replies are inline up to ~5; long reply
    chains need extra comments.list calls).

    `order` is one of "time" (newest first) or "relevance" (top
    comments). We default to "time" - the archiver's job is to catch
    new comments before they get deleted, not to surface popular ones.
    The UI can re-sort by saved like_count later.
    """
    service = build("youtube", "v3", credentials=creds, cache_discovery=False)
    out: list = []

    page_token = None
    while True:
        req = service.commentThreads().list(
            part="snippet,replies",
            videoId=video_id,
            order=order,
            maxResults=100,
            pageToken=page_token,
            textFormat="plainText",
        )
        try:
            resp = req.execute()
        except HttpError as e:
            # Differentiate "expected" empty-result cases from real
            # failures. commentsDisabled / videoNotFound / forbidden
            # are legitimate "no comments to fetch" states - return
            # an empty list so the rescan engine just marks the video
            # synced and moves on, rather than blowing up the run.
            reason = _extract_http_error_reason(e)
            if reason in {
                "commentsDisabled",
                "videoNotFound",
                "forbidden",
                "commentsNotEnabled",
            }:
                return []
            raise
        for thread in resp.get("items", []):
            top = thread.get("snippet", {}).get("topLevelComment", {})
            top_snippet = top.get("snippet", {})
            top_id = top.get("id")
            if not top_id:
                continue
            out.append(_normalize_comment(top_id, top_snippet, parent_id=None))

            # Inline replies, when YouTube returns them. May be a subset
            # of all replies if there are more than ~5.
            for reply in (thread.get("replies") or {}).get("comments") or []:
                rid = reply.get("id")
                if not rid:
                    continue
                out.append(
                    _normalize_comment(
                        rid, reply.get("snippet", {}), parent_id=top_id,
                    )
                )

            total_reply_count = int(top_snippet.get("totalReplyCount") or 0)
            inline_count = len(
                (thread.get("replies") or {}).get("comments") or []
            )
            if total_reply_count > inline_count:
                # Page through the remaining replies via comments.list.
                _append_all_replies(service, out, parent_id=top_id)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return out


def _extract_http_error_reason(err: HttpError) -> Optional[str]:
    """Pull the 'reason' tag out of a Google HttpError so callers can
    treat e.g. commentsDisabled differently from a transient 5xx."""
    try:
        import json as _json
        body = _json.loads(err.content.decode("utf-8")) if err.content else {}
        errors = (body.get("error") or {}).get("errors") or []
        if errors:
            return errors[0].get("reason")
    except Exception:  # noqa: BLE001
        pass
    return None


def _append_all_replies(service, out: list, *, parent_id: str) -> None:
    """Append every reply to a thread to out, paging if necessary.

    Used when commentThreads.replies returned a partial list (more
    replies exist than fit inline). Each comments.list call is another
    quota unit.

    We re-fetch from scratch rather than trying to deduplicate against
    inline replies - simpler, and the diff engine de-dupes by ID
    anyway.
    """
    # Clear any inline-fetched replies we already pushed so the canonical
    # paged list below replaces them. (Simpler than dedup-on-merge.)
    out_no_inline = [r for r in out if r.get("parent_id") != parent_id]
    out.clear()
    out.extend(out_no_inline)

    page_token = None
    while True:
        resp = service.comments().list(
            part="snippet",
            parentId=parent_id,
            maxResults=100,
            pageToken=page_token,
            textFormat="plainText",
        ).execute()
        for reply in resp.get("items", []):
            rid = reply.get("id")
            if not rid:
                continue
            out.append(
                _normalize_comment(rid, reply.get("snippet", {}), parent_id=parent_id)
            )
        page_token = resp.get("nextPageToken")
        if not page_token:
            break


def _normalize_comment(
    comment_id: str, snippet: Dict[str, Any], *, parent_id: Optional[str]
) -> Dict[str, Any]:
    """Flatten the YouTube commentThreads/comments item shape into the
    dict the rescan engine expects. Both top-level and reply items share
    the same `snippet` schema at this layer."""
    author_channel = snippet.get("authorChannelId") or {}
    author_channel_id = (
        author_channel.get("value")
        if isinstance(author_channel, dict)
        else None
    )
    published_at = snippet.get("publishedAt")
    updated_at = snippet.get("updatedAt")
    return {
        "id": comment_id,
        "parent_id": parent_id,
        "author": snippet.get("authorDisplayName") or "",
        "author_channel_id": author_channel_id,
        "text": snippet.get("textDisplay") or "",
        "like_count": int(snippet.get("likeCount") or 0),
        "published_at": published_at,
        "updated_at": updated_at,
        "is_edited": (
            published_at is not None
            and updated_at is not None
            and updated_at != published_at
        ),
        "is_pinned": False,  # YouTube doesn't surface "pinned" on the API
        "is_by_uploader": (
            snippet.get("authorChannelUrl", "") != ""
            and snippet.get("canRate") is False
            # Heuristic only. The reliable signal is comparing
            # authorChannelId to the video's channelId at the caller -
            # we don't have that here. Leave False here; caller can
            # patch True after they've matched IDs.
        ),
        "viewer_rating_like": (
            snippet.get("viewerRating") == "like"
        ),
    }
