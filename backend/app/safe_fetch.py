"""Fetching images from YouTube, and nothing else.

The server downloads images to archive them: channel avatars and video
thumbnails. An avatar's url used to come straight from the client
(``avatarUrl`` in the channel payload, sent back on every save) and was fetched
as-is - any scheme, any host, redirects followed, the whole body read into
memory. A signed-in user could point it at localhost, a private address or the
cloud metadata service and read the response back as their avatar. GitHub's
code scanning flagged it critical (alert #14).

Every image fetch goes through here instead: https to YouTube's image hosts
only, redirects followed by hand and re-checked on every hop, a size cap, and
an overall deadline rather than only a per-read timeout.
"""
from __future__ import annotations

import time
from typing import Optional, Tuple
from urllib.parse import urljoin, urlsplit

import requests

# The hosts YouTube serves avatars and thumbnails from. Deliberately not
# youtube.com: it is not an image host, and it serves pages.
IMAGE_HOST_SUFFIXES = ("ggpht.com", "googleusercontent.com", "ytimg.com")

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 3
_REDIRECT_CODES = (301, 302, 303, 307, 308)


class UnsafeFetch(ValueError):
    """Refused before or during the fetch: wrong host, too big, too slow."""


def is_youtube_image_url(url: object) -> bool:
    """True only for an https url on one of YouTube's image hosts."""
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    return (
        parts.scheme == "https"
        and port in (None, 443)
        and not parts.username
        and not parts.password
        and any(host == s or host.endswith("." + s) for s in IMAGE_HOST_SUFFIXES)
    )


def _follow(method, url: str, *, timeout_seconds: float, stream: bool):
    """Issue the request, following redirects by hand so every hop is checked
    against the allowlist - a YouTube url that redirects somewhere else is
    refused at the redirect. Returns the final response; the caller closes it."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if not is_youtube_image_url(current):
            raise UnsafeFetch("refusing to fetch a url that is not a YouTube image")
        resp = method(
            current, timeout=timeout_seconds, allow_redirects=False, stream=stream
        )
        if resp.status_code in _REDIRECT_CODES:
            location = resp.headers.get("Location") or resp.headers.get("location")
            resp.close()
            if not location:
                raise UnsafeFetch("redirect without a location")
            current = urljoin(current, location)
            continue
        return resp
    raise UnsafeFetch("too many redirects")


def fetch_image(
    url: str, *, timeout_seconds: float = 30.0
) -> Tuple[bytes, Optional[str]]:
    """GET an image. Returns ``(bytes, content_type)``.

    Raises UnsafeFetch for a refused url, an oversized body or a blown
    deadline, and requests.HTTPError for anything other than a 200 - which
    raise_for_status alone would not do, since it lets 3xx through.
    """
    deadline = time.monotonic() + timeout_seconds
    resp = _follow(requests.get, url, timeout_seconds=timeout_seconds, stream=True)
    try:
        if resp.status_code != 200:
            raise requests.HTTPError(
                f"unexpected status {resp.status_code}", response=resp
            )
        declared = resp.headers.get("Content-Length")
        if declared and str(declared).isdigit() and int(declared) > MAX_IMAGE_BYTES:
            raise UnsafeFetch("image too large")
        chunks = []
        total = 0
        for chunk in resp.iter_content(64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_IMAGE_BYTES:
                raise UnsafeFetch("image too large")
            if time.monotonic() > deadline:
                raise UnsafeFetch("image download took too long")
            chunks.append(chunk)
        return b"".join(chunks), resp.headers.get("Content-Type")
    finally:
        resp.close()


def head_image(
    url: str, *, timeout_seconds: float = 5.0
) -> Optional[requests.Response]:
    """HEAD an image url. None when the url is refused or unreachable."""
    try:
        resp = _follow(requests.head, url, timeout_seconds=timeout_seconds, stream=False)
    except (UnsafeFetch, requests.RequestException):
        return None
    resp.close()
    return resp
