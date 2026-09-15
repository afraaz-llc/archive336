"""Fixes for GitHub code scanning alerts #14 (SSRF), #15/#16 (cookies) and
#17 (PubSub XML), plus the session cookie's missing Secure flag.

Every test that refuses something asserts the network was never touched: a
guard that fetches first and complains afterwards is not a guard.
"""
from __future__ import annotations

import hashlib
import hmac

import pytest
import requests
from fastapi import Response
from starlette.requests import Request

from app import pubsub, r2, safe_fetch, security
from app.routes import youtube as yt


class _Resp:
    def __init__(self, status=200, body=b"img", headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {"Content-Type": "image/jpeg"}
        self.closed = False

    def iter_content(self, n):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]

    def close(self):
        self.closed = True


@pytest.fixture
def network(monkeypatch):
    """Record every outbound request and answer from a script."""
    calls = []
    script = {}

    def fake(url, **kw):
        calls.append(url)
        if kw.get("allow_redirects") is not False:
            raise AssertionError("redirects must be followed by hand")
        return script.get(url, _Resp())

    monkeypatch.setattr(safe_fetch.requests, "get", fake)
    monkeypatch.setattr(safe_fetch.requests, "head", fake)
    return calls, script


# ---- SSRF (#14) ---------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",
    "http://127.0.0.1:8000/api/health",
    "https://127.0.0.1/",
    "http://yt3.ggpht.com/a.jpg",
    "https://yt3.ggpht.com:8443/a.jpg",
    "https://evil.example/yt3.ggpht.com.jpg",
    "https://ggpht.com.evil.example/a.jpg",
    "https://user:pw@yt3.ggpht.com/a.jpg",
    "https://www.youtube.com/channel/UCx",
    "file:///etc/passwd",
])
def test_non_youtube_image_urls_are_refused_without_a_request(network, url):
    calls, _ = network
    with pytest.raises(safe_fetch.UnsafeFetch):
        safe_fetch.fetch_image(url)
    assert calls == []


def test_a_youtube_image_is_fetched(network):
    calls, script = network
    script["https://yt3.ggpht.com/a.jpg"] = _Resp(body=b"\xff\xd8jpeg")
    data, ct = safe_fetch.fetch_image("https://yt3.ggpht.com/a.jpg")
    assert data == b"\xff\xd8jpeg" and ct == "image/jpeg"
    assert calls == ["https://yt3.ggpht.com/a.jpg"]


def test_a_redirect_off_youtube_is_refused_at_the_hop(network):
    calls, script = network
    script["https://yt3.ggpht.com/a.jpg"] = _Resp(
        status=302, headers={"Location": "http://169.254.169.254/latest/meta-data/"}
    )
    with pytest.raises(safe_fetch.UnsafeFetch):
        safe_fetch.fetch_image("https://yt3.ggpht.com/a.jpg")
    assert calls == ["https://yt3.ggpht.com/a.jpg"], "the metadata service was never asked"


def test_a_redirect_between_youtube_image_hosts_still_works(network):
    calls, script = network
    script["https://yt3.ggpht.com/a.jpg"] = _Resp(
        status=301, headers={"Location": "https://yt3.googleusercontent.com/a.jpg"}
    )
    script["https://yt3.googleusercontent.com/a.jpg"] = _Resp(body=b"ok")
    assert safe_fetch.fetch_image("https://yt3.ggpht.com/a.jpg")[0] == b"ok"


def test_an_oversized_image_is_refused(network, monkeypatch):
    _, script = network
    monkeypatch.setattr(safe_fetch, "MAX_IMAGE_BYTES", 10)
    script["https://i.ytimg.com/vi/x/hq.jpg"] = _Resp(body=b"x" * 11)
    with pytest.raises(safe_fetch.UnsafeFetch):
        safe_fetch.fetch_image("https://i.ytimg.com/vi/x/hq.jpg")


def test_only_a_200_counts(network):
    _, script = network
    script["https://i.ytimg.com/vi/x/hq.jpg"] = _Resp(status=204, body=b"")
    with pytest.raises(requests.HTTPError):
        safe_fetch.fetch_image("https://i.ytimg.com/vi/x/hq.jpg")


def test_download_to_r2_writes_nothing_for_a_refused_url(network, monkeypatch):
    calls, _ = network
    puts = []

    class _Boto:
        def put_object(self, **kw):
            puts.append(kw)

    monkeypatch.setattr(r2, "client", lambda: _Boto())
    monkeypatch.setattr(r2, "_bucket", "bucket")
    monkeypatch.setattr(r2, "_record", lambda *a, **k: None)

    with pytest.raises(safe_fetch.UnsafeFetch):
        r2.download_to_r2("http://169.254.169.254/", "users/u/avatar.jpg", subject="u")
    assert calls == [] and puts == []


def test_a_client_sent_avatar_url_that_is_not_youtube_is_never_kept():
    payload = {"avatarUrl": "http://127.0.0.1:8000/", "settings": {"saveChannelAvatar": False}}
    yt._resolve_avatar(payload, "UCx", existing={"avatarUrl": "https://yt3.googleusercontent.com/a"})
    assert payload["avatarUrl"] == "https://yt3.googleusercontent.com/a"

    payload = {"avatarUrl": "http://169.254.169.254/", "settings": {"saveChannelAvatar": False}}
    yt._resolve_avatar(payload, "UCx", existing=None)
    assert payload["avatarUrl"] == ""

    payload = {"avatarUrl": "https://yt3.ggpht.com/real", "settings": {"saveChannelAvatar": False}}
    yt._resolve_avatar(payload, "UCx", existing=None)
    assert payload["avatarUrl"] == "https://yt3.ggpht.com/real"


# ---- PubSub (#17) -------------------------------------------------------

_FEED = (
    b'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">'
    b"<entry><yt:videoId>abc12345678</yt:videoId><yt:channelId>UCaaaaaaaaaaaaaaaaaaaaaa</yt:channelId>"
    b"<title>t</title><published>2026-09-15T00:00:00+00:00</published>"
    b"<updated>2026-09-15T00:00:00+00:00</updated></entry></feed>"
)


def test_a_normal_feed_still_parses():
    entries = pubsub.parse_notification(_FEED)
    assert len(entries) == 1 and entries[0]["video_id"] == "abc12345678"


def test_a_feed_using_entities_is_refused():
    """Parses to one entry with the stdlib parser; refused by the defused one."""
    body = (
        b'<?xml version="1.0"?><!DOCTYPE feed [<!ENTITY x "UCaaaaaaaaaaaaaaaaaaaaaa">]>'
        + _FEED.replace(b"UCaaaaaaaaaaaaaaaaaaaaaa", b"&x;")
    )
    assert pubsub.parse_notification(body) == []


def test_no_secret_means_nothing_is_accepted(monkeypatch):
    monkeypatch.delenv("PUBSUB_SECRET", raising=False)
    monkeypatch.delenv("PUBSUB_ALLOW_UNSIGNED", raising=False)
    assert pubsub.verify_signature(_FEED, None) is False

    monkeypatch.setenv("PUBSUB_ALLOW_UNSIGNED", "1")
    assert pubsub.verify_signature(_FEED, None) is True, "explicit local-dev opt-in"


def test_a_signed_notification_is_verified(monkeypatch):
    monkeypatch.setenv("PUBSUB_SECRET", "s3cret")
    good = "sha256=" + hmac.new(b"s3cret", _FEED, hashlib.sha256).hexdigest()
    assert pubsub.verify_signature(_FEED, good) is True
    assert pubsub.verify_signature(_FEED, "sha256=" + "0" * 64) is False
    assert pubsub.verify_signature(_FEED, None) is False


# ---- cookies (#15, #16, and Secure) --------------------------------------

_TOKEN = "A" * 43


def _request(cookie_header):
    return Request({"type": "http", "headers": [(b"cookie", cookie_header.encode())]})


def _set_cookie_headers(response):
    return [v.decode() for k, v in response.raw_headers if k == b"set-cookie"]


def test_only_token_shaped_values_survive_the_linked_cookie():
    req = _request(f'{security.LINKED_COOKIE_NAME}={_TOKEN},not-a-token!,short,{"B" * 43}')
    assert security.get_linked_tokens(req) == [_TOKEN, "B" * 43]


def test_a_non_token_is_never_written_as_the_session():
    resp = Response()
    security.set_session_cookie(resp, "not a token; Path=/evil")
    assert _set_cookie_headers(resp) == []


def test_session_and_linked_cookies_are_secure_and_httponly():
    resp = Response()
    security.set_session_cookie(resp, _TOKEN)
    security.set_linked_cookie(resp, [_TOKEN])
    headers = _set_cookie_headers(resp)
    assert len(headers) == 2
    for h in headers:
        assert "Secure" in h and "HttpOnly" in h and "SameSite=lax" in h


# ---- admin snapshots (#18, #19, #20) ------------------------------------


def test_admin_snapshots_report_a_failure_without_its_exception_text():
    """The snapshot dicts reach the admin page. An exception message can carry
    anything the failing library put in it, so only a generic line goes out;
    the detail goes to the server log."""
    from app import billing, mercury

    for module in (billing, mercury):
        line = module._reported_error("account", RuntimeError("sk_live_abc123 in a message"))
        assert line.startswith("account:")
        assert "sk_live" not in line
