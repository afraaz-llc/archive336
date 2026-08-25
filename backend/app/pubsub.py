"""PubSubHubbub integration with YouTube's feed hub.

YouTube exposes a PuSH (PubSubHubbub) feed for every channel at:

    https://www.youtube.com/xml/feeds/videos.xml?channel_id=UCxxxx

The Google-run hub at https://pubsubhubbub.appspot.com/ lets a
subscriber (us) receive an HTTP POST with an Atom feed entry within
seconds of every new upload. Massively better than polling -
discovery latency drops from "next cron run" to "as soon as YouTube
publishes the entry."

This module is the integration layer:

  subscribe_channel(channel)        -> POST to the hub asking it to
                                       notify us when this channel
                                       uploads. Stamps
                                       pubsub_lease_expires_at when
                                       the hub confirms.
  unsubscribe_channel(channel)      -> POST asking the hub to stop.
  verify_signature(body, header)    -> validate X-Hub-Signature on a
                                       notification using PUBSUB_SECRET.
  parse_notification(body)          -> Atom feed -> list of
                                       (channel_id, video_id, title,
                                       published_at) tuples.

The callback URL is built from the BASE_URL env variable so dev/staging
boxes don't try to register their own callback against the production
hub registration.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen


log = logging.getLogger("archive336.pubsub")


# Google's hub for YouTube feeds. Public, no auth required.
HUB_URL = "https://pubsubhubbub.appspot.com/subscribe"


def _topic_url(youtube_channel_id: str) -> str:
    """Feed URL that the hub watches on our behalf."""
    return (
        f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={youtube_channel_id}"
    )


def _callback_url() -> Optional[str]:
    """Where the hub sends notifications. BASE_URL must be a
    publicly-reachable HTTPS origin; we never register a localhost
    callback because the hub couldn't reach it anyway."""
    base = (os.environ.get("BASE_URL") or "").rstrip("/")
    if not base or not base.startswith("https://"):
        return None
    return f"{base}/api/youtube/pubsub-callback"


def _shared_secret() -> Optional[str]:
    """HMAC key used by the hub to sign notifications + by us to
    verify them. Optional but strongly recommended - without it,
    anyone on the internet who guesses the callback URL can forge
    notifications. Set PUBSUB_SECRET in /opt/aether/.env."""
    s = os.environ.get("PUBSUB_SECRET")
    return s if s else None


def _post_hub(mode: str, youtube_channel_id: str) -> Tuple[int, str]:
    """Send a subscribe or unsubscribe request to the hub. Returns
    (status_code, response_body) so callers can log + decide what
    to do on failure."""
    callback = _callback_url()
    if callback is None:
        raise RuntimeError(
            "BASE_URL env not set to a https:// origin; PubSub hub "
            "can't reach a localhost callback. Set BASE_URL in "
            "/opt/aether/.env to the production origin."
        )
    body_params = {
        "hub.callback": callback,
        "hub.topic": _topic_url(youtube_channel_id),
        "hub.verify": "async",
        "hub.mode": mode,
        # 10 days is the hub's default + maximum lease. We re-sub
        # before this expires via the renewal cron.
        "hub.lease_seconds": "864000",
    }
    secret = _shared_secret()
    if secret:
        body_params["hub.secret"] = secret
    data = urlencode(body_params).encode("utf-8")
    req = Request(
        HUB_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        # urlopen on a 2xx returns normally; non-2xx raises HTTPError
        # (a subclass) and exposes .code. Capture both shapes here.
        code = getattr(e, "code", 0)
        body = ""
        try:
            body = e.read().decode("utf-8", errors="ignore")  # type: ignore[attr-defined]
        except Exception:
            body = str(e)
        log.warning("pubsub hub %s for %s failed: %s %s", mode, youtube_channel_id, code, body)
        return int(code or 0), body


def subscribe_channel(youtube_channel_id: str) -> bool:
    """Ask the hub to start notifying us about uploads on this channel.
    Returns True iff the hub accepted the request (HTTP 202). The
    actual verification handshake happens asynchronously on our
    callback endpoint; success here just means the hub queued the
    verify."""
    status, body = _post_hub("subscribe", youtube_channel_id)
    ok = status == 202
    log.info(
        "pubsub subscribe %s: %s %s",
        youtube_channel_id,
        status,
        "ok" if ok else body[:200],
    )
    return ok


def unsubscribe_channel(youtube_channel_id: str) -> bool:
    """Ask the hub to stop notifying us. Same shape as subscribe."""
    status, body = _post_hub("unsubscribe", youtube_channel_id)
    ok = status == 202
    log.info(
        "pubsub unsubscribe %s: %s %s",
        youtube_channel_id,
        status,
        "ok" if ok else body[:200],
    )
    return ok


def verify_signature(body: bytes, header_value: Optional[str]) -> bool:
    """Verify the X-Hub-Signature header on an inbound notification.

    Header format: "sha1=<hex>"  (older hubs) or "sha256=<hex>".

    Without a configured PUBSUB_SECRET, returns False on any
    signed request (we shouldn't be receiving signed notifications
    we can't verify) and True on unsigned ones (development-mode
    operator chose to trust the hub). The production guidance is
    always: set PUBSUB_SECRET.
    """
    secret = _shared_secret()
    if not secret:
        # No secret configured. Only accept unsigned notifications.
        # Receiving a signed one we can't verify is a configuration
        # bug worth surfacing.
        return header_value is None
    if header_value is None:
        return False
    try:
        algo_name, _, expected_hex = header_value.partition("=")
        algo_name = algo_name.strip().lower()
        if algo_name == "sha1":
            digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha1).hexdigest()
        elif algo_name == "sha256":
            digest = hmac.new(
                secret.encode("utf-8"), body, hashlib.sha256
            ).hexdigest()
        else:
            return False
        return hmac.compare_digest(digest, expected_hex.strip().lower())
    except Exception:
        return False


# XML namespace map for the Atom feed YouTube sends. The 'yt' prefix
# is YouTube-specific; 'atom' is the standard Atom namespace.
_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}


def parse_notification(body: bytes) -> List[dict]:
    """Parse the Atom feed payload of a hub notification.

    Returns a list of {channel_id, video_id, title, published_at,
    updated_at} dicts, one per entry. A typical upload notification
    has exactly one entry; deletions/edits may also arrive here with
    the same shape.

    Returns [] if the payload doesn't parse - we'd rather drop a
    malformed notification on the floor than 500 the hub (which
    would retry forever).
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        log.warning("pubsub notification didn't parse as XML")
        return []

    entries = root.findall("atom:entry", _NS)
    out: List[dict] = []
    for e in entries:
        video_id_el = e.find("yt:videoId", _NS)
        channel_id_el = e.find("yt:channelId", _NS)
        title_el = e.find("atom:title", _NS)
        published_el = e.find("atom:published", _NS)
        updated_el = e.find("atom:updated", _NS)

        if video_id_el is None or channel_id_el is None:
            continue

        out.append(
            {
                "channel_id": channel_id_el.text or "",
                "video_id": video_id_el.text or "",
                "title": (title_el.text or "") if title_el is not None else "",
                "published_at": _parse_iso(
                    published_el.text if published_el is not None else None
                ),
                "updated_at": _parse_iso(
                    updated_el.text if updated_el is not None else None
                ),
            }
        )
    return out


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None
