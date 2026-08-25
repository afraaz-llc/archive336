"""Light-touch HTML scraping for YouTube channel metadata.

For now this pulls the profile picture URL and the About-tab fields. As we
wire more, we'll grow this (or swap to yt-dlp / the Data API when we hit
limits).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, NamedTuple, Optional, Tuple
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


# YouTube serves a stripped-down page to unrecognized agents. A normal-looking
# desktop UA gets the full HTML with og: meta tags + ytInitialData filled in.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Ask for English. Without this YouTube localises by where the request came
# from, and our box is in Germany - so every page, every count string and every
# playabilityStatus reason came back in German. Parsers that key off prose
# (and number formats that flip "." and ",") quietly break on that.
_ACCEPT_LANGUAGE = "en-US,en;q=0.9"

_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": _ACCEPT_LANGUAGE,
}

_OG_IMAGE_RE = re.compile(rb'<meta property="og:image" content="([^"]+)"')
_YT_INITIAL_DATA_RE = re.compile(rb"var ytInitialData = ")


def _channel_url_for(handle_or_id: str, suffix: str = "") -> Optional[str]:
    """Build the canonical channel URL for any of YouTube's id shapes."""
    s = handle_or_id.strip()
    if not s:
        return None
    if s.startswith("@"):
        base = f"https://www.youtube.com/{s}"
    elif s.startswith("UC"):
        base = f"https://www.youtube.com/channel/{s}"
    else:
        base = f"https://www.youtube.com/@{s}"
    return base + suffix


def _english(url: str) -> str:
    """Pin a YouTube URL to the English locale.

    Accept-Language is only advisory and YouTube leans on the request's
    geography when it disagrees. hl/gl are what YouTube's own language picker
    sets, so they win over the edge's guess about who we are.
    """
    if "hl=" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}hl=en&gl=US"


def _fetch_html(url: str, timeout: float = 5.0) -> Optional[bytes]:
    req = Request(_english(url), headers=_DEFAULT_HEADERS)
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (URLError, TimeoutError, ConnectionError, OSError):
        return None
    except Exception:
        return None


def _extract_yt_initial_data(html: bytes) -> Optional[Any]:
    """Pull the `var ytInitialData = {...}` blob out of a YouTube page."""
    m = _YT_INITIAL_DATA_RE.search(html)
    if m is None:
        return None
    try:
        s = html[m.end():].decode("utf-8", errors="ignore")
        obj, _ = json.JSONDecoder().raw_decode(s)
        return obj
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _find_first(data: Any, key: str) -> Any:
    """Walk a nested dict/list and return the first value of `key` found."""
    if isinstance(data, dict):
        if key in data:
            return data[key]
        for v in data.values():
            r = _find_first(v, key)
            if r is not None:
                return r
    elif isinstance(data, list):
        for item in data:
            r = _find_first(item, key)
            if r is not None:
                return r
    return None


def resolve_channel_id(
    handle_or_id: str,
    timeout: float = 5.0,
) -> Optional[str]:
    """Resolve any of YouTube's identifier shapes to the canonical UC
    channel id. Accepts @handles, /c/custom, /user/legacy, or a UC id.

    Returns None on any failure (HTTP error, missing metadata, etc.).
    Caller is expected to surface a user-friendly 4xx error in that
    case.
    """
    s = (handle_or_id or "").strip()
    if not s:
        return None
    # Already a canonical UC id - nothing to do.
    if s.startswith("UC") and len(s) >= 20:
        return s
    url = _channel_url_for(s)
    if url is None:
        return None
    html = _fetch_html(url, timeout=timeout)
    if html is None:
        return None
    data = _extract_yt_initial_data(html)
    if data is not None:
        metadata = _find_first(data, "channelMetadataRenderer") or {}
        # YouTube has used two key names over time. externalId is the
        # current one (as of 2026-05); externalChannelId still appears
        # in some legacy serializations. Try both before falling back
        # to the regex.
        for key in ("externalId", "externalChannelId"):
            v = metadata.get(key)
            if isinstance(v, str) and v.startswith("UC"):
                return v
    # Fallback: scrape any UC id directly out of the page HTML. The
    # page peppers "externalId":"UC…" and "browseId":"UC…" in several
    # places; either is fine because both point at the same channel.
    for pat in (
        rb'"externalId":"(UC[\w\-]+)"',
        rb'"channelId":"(UC[\w\-]+)"',
        rb'"browseId":"(UC[\w\-]+)"',
    ):
        m = re.search(pat, html)
        if m:
            return m.group(1).decode("ascii", errors="ignore")
    return None


def fetch_channel_avatar_url(
    handle_or_id: str,
    timeout: float = 5.0,
) -> Optional[str]:
    """Fetch a channel page and return the profile picture URL.

    Returns None on any failure - caller should fall back to a placeholder.
    """
    url = _channel_url_for(handle_or_id)
    if url is None:
        return None
    html = _fetch_html(url, timeout=timeout)
    if html is None:
        return None
    m = _OG_IMAGE_RE.search(html)
    if m is None:
        return None
    return m.group(1).decode("utf-8", errors="ignore")


def _parse_link(entry: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Pull a {label, url} dict out of one channelExternalLinkViewModel entry."""
    vm = entry.get("channelExternalLinkViewModel")
    if not isinstance(vm, dict):
        return None

    label = ""
    title = vm.get("title")
    if isinstance(title, dict):
        label = title.get("content", "") or ""

    url = ""
    link = vm.get("link", {})
    # Prefer the redirect target (it's the full URL with scheme).
    if isinstance(link, dict):
        runs = link.get("commandRuns")
        if isinstance(runs, list) and runs:
            try:
                redirect = runs[0]["onTap"]["innertubeCommand"]["urlEndpoint"]["url"]
                qs = parse_qs(urlparse(redirect).query)
                if "q" in qs and qs["q"]:
                    url = qs["q"][0]
            except (KeyError, TypeError, IndexError):
                pass
        # Fall back to display content (prefix https:// if missing).
        if not url:
            c = link.get("content", "") or ""
            if c:
                url = c if c.startswith("http") else f"https://{c}"

    if not label and not url:
        return None
    return {"label": label, "url": url}


def _parse_joined_date(text: str) -> Optional[str]:
    """Parse YouTube's 'Joined Jul 21, 2010' into ISO 'YYYY-MM-DD'."""
    if not text:
        return None
    m = re.match(r"Joined\s+(\w+\s+\d+,\s+\d+)", text)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%b %d, %Y").date().isoformat()
    except ValueError:
        return None


_COUNT_RE = re.compile(r"^([\d,.]+)\s*([KMB])?", re.IGNORECASE)


def _normalise_number(num_str: str, has_suffix: bool) -> str:
    """Turn a possibly-European number token into something float() accepts.

    Which of "." and "," is the decimal point depends on the page's locale, so
    decide from the shape of the token rather than assuming English:
      - both separators present -> the rightmost one is the decimal point
      - repeated separator      -> it groups digits ("1.162.345.518")
      - a lone separator with exactly three digits behind it and no magnitude
        suffix -> it groups digits ("1.510 Videos"), because English only ever
        puts a decimal point in front of a suffix and it keeps one digit there
      - a lone comma            -> decimal only when a magnitude suffix
                                   follows, because English writes "20.6M" and
                                   never "20,6M", but does write "1,234"
    """
    has_dot = "." in num_str
    has_comma = "," in num_str
    if has_dot and has_comma:
        decimal = "." if num_str.rindex(".") > num_str.rindex(",") else ","
    elif has_dot:
        tail = num_str.rsplit(".", 1)[1]
        grouped = num_str.count(".") > 1 or (not has_suffix and len(tail) == 3)
        decimal = "" if grouped else "."
    elif has_comma:
        decimal = "," if (has_suffix and num_str.count(",") == 1) else ""
    else:
        return num_str
    out = "".join(
        c for c in num_str if c.isdigit() or (decimal and c == decimal)
    )
    return out.replace(",", ".") if decimal == "," else out


def _parse_count(text: str) -> int:
    """Parse YouTube's count strings like '20.6M subscribers' or '4,216,239,788 views'.

    We now ask YouTube for English (see _ACCEPT_LANGUAGE / _english), so the
    English shapes are what should arrive. European shapes are still handled
    because locale negotiation is not a guarantee and the failure was ugly:
    the German-hosted box read "45,8 Mio. Abonnenten" as 458 million (the "M"
    of "Mio." matched the magnitude suffix) and "1.162.345.518 Aufrufe" as 0.

    Only the digits are locale-proofed, not the magnitude WORD: a German
    "45,8 Mrd." still reads as 45.8 million, since its "M" matches before we
    ever see the "rd". Translating those is a rabbit hole (Spanish "mil" is a
    thousand, Portuguese "mi" a million), so the English request is the fix
    and this is only the guardrail behind it.
    """
    if not text:
        return 0
    m = _COUNT_RE.match(text.strip())
    if not m:
        return 0
    suffix = (m.group(2) or "").upper()
    num_str = _normalise_number(m.group(1), bool(suffix))
    try:
        num = float(num_str)
    except ValueError:
        return 0
    if suffix:
        multipliers = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
        num *= multipliers.get(suffix, 1)
    return int(num)


def fetch_channel_stats(
    handle_or_id: str,
    timeout: float = 5.0,
) -> Optional[Dict[str, int]]:
    """Fetch live channel stats (subscribers, video count, total views).

    Returns dict or None if the page fetch fails entirely.
    """
    url = _channel_url_for(handle_or_id, suffix="/about")
    if url is None:
        return None
    html = _fetch_html(url, timeout=timeout)
    if html is None:
        return None
    data = _extract_yt_initial_data(html)
    if data is None:
        return None

    about = _find_first(data, "aboutChannelViewModel") or {}
    return {
        "subscriberCount": _parse_count(about.get("subscriberCountText", "") or ""),
        "videoCount": _parse_count(about.get("videoCountText", "") or ""),
        "totalViews": _parse_count(about.get("viewCountText", "") or ""),
    }


def _find_all(data: Any, key: str, out: Optional[List[Any]] = None) -> List[Any]:
    """Walk a nested dict/list and collect every value for `key`."""
    if out is None:
        out = []
    if isinstance(data, dict):
        if key in data:
            out.append(data[key])
        for v in data.values():
            _find_all(v, key, out)
    elif isinstance(data, list):
        for item in data:
            _find_all(item, key, out)
    return out


def _pick_largest_thumbnail(thumbnails: Any) -> str:
    """Pick the largest thumbnail URL from a YouTube thumbnails array."""
    if not isinstance(thumbnails, list) or not thumbnails:
        return ""
    largest = max(
        thumbnails,
        key=lambda t: (t.get("width", 0) or 0) * (t.get("height", 0) or 0)
        if isinstance(t, dict)
        else 0,
    )
    if isinstance(largest, dict):
        return largest.get("url", "") or ""
    return ""


def fetch_channel_videos(
    handle_or_id: str,
    timeout: float = 8.0,
    max_pages: int = 30,
) -> Optional[List[Dict[str, Any]]]:
    """Fetch the full video catalog for a channel via yt-dlp.

    YouTube moved channel video grids off `videoRenderer` in mid-2026,
    which silently killed the hand-rolled InnerTube parser that used to
    live here (it returned [] for every channel). Rather than chase
    YouTube's markup again, enumerate the channel's /videos tab with
    yt-dlp in flat-playlist mode - the yt-dlp project absorbs YouTube's
    structural churn for us. Metadata only: no download, no cookies,
    public videos only.

    The /videos TAB, not the UU uploads playlist: as of mid-2026 YouTube
    caps playlist continuations at 100 entries (verified from both
    datacenter and residential IPs, latest yt-dlp), while the tab
    paginates the full catalog. The tab also matches the old parser's
    scope exactly (regular videos - no shorts/lives).

    Parity notes vs the old parser:
    - uploadDate comes from yt-dlp's approximate_date extractor arg,
      derived from YouTube's relative "3 weeks ago" text - the same
      approximation the old parser made. Date-only ISO (2026-06-28).
    - viewCount isn't present in flat tab entries, so it's 0 until
      a sync fills in real metadata.
    - max_pages kept for signature parity: cap = max_pages * 30 entries,
      matching the old ~900-video ceiling.

    Returns None if the channel can't be reached. Returns [] if the
    channel exists but has no public videos.
    """
    cid = resolve_channel_id(handle_or_id, timeout=timeout)
    if cid is None:
        return None

    # Lazy import - yt-dlp is a heavy module and only this function
    # needs it; keep app startup (and the other scrapers) light.
    from yt_dlp import YoutubeDL  # noqa: WPS433
    from yt_dlp.utils import DownloadError  # noqa: WPS433

    opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
        "playlistend": max_pages * 30,
        "socket_timeout": timeout,
        # Synthesize approximate upload timestamps from YouTube's
        # relative dates ("3 weeks ago") in flat mode.
        "extractor_args": {"youtubetab": {"approximate_date": [""]}},
    }
    url = f"https://www.youtube.com/channel/{cid}/videos"
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as e:
        msg = str(e).lower()
        # A channel with zero public uploads has no videos tab at all
        # ("This channel does not have a videos tab"). That's "no public
        # videos", not "YouTube unreachable".
        if "does not have a" in msg or "does not exist" in msg:
            return []
        return None
    except Exception:
        return None
    if not isinstance(info, dict):
        return None

    out: List[Dict[str, Any]] = []
    seen: set = set()
    for entry in info.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        vid = str(entry.get("id") or "").strip()
        if not vid or vid in seen:
            continue
        seen.add(vid)
        upload_iso = ""
        ts = entry.get("timestamp")
        if isinstance(ts, (int, float)) and ts > 0:
            upload_iso = (
                datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
            )
        thumbnail = _pick_largest_thumbnail(entry.get("thumbnails"))
        if not thumbnail:
            thumbnail = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        duration = entry.get("duration")
        out.append(
            {
                "id": vid,
                "title": str(entry.get("title") or "") or vid,
                "description": str(entry.get("description") or ""),
                "uploadDate": upload_iso,
                "durationSec": int(duration)
                if isinstance(duration, (int, float))
                else 0,
                "thumbnailUrl": thumbnail,
                "viewCount": int(entry.get("view_count") or 0),
            }
        )
    return out


_PLAYABILITY_STATUS_RE = re.compile(
    r'"playabilityStatus"\s*:\s*\{[^}]*?"status"\s*:\s*"([^"]+)"'
)
_PLAYABILITY_REASON_RE = re.compile(
    r'"playabilityStatus"\s*:\s*\{[^}]*?"reason"\s*:\s*"([^"]+)"'
)
_YT_INITIAL_PLAYER_RESPONSE_RE = re.compile(
    rb"var ytInitialPlayerResponse = "
)


def _extract_yt_initial_player_response(html: bytes) -> Optional[Dict[str, Any]]:
    """Pull the `var ytInitialPlayerResponse = {...}` blob out of a watch page."""
    m = _YT_INITIAL_PLAYER_RESPONSE_RE.search(html)
    if m is None:
        return None
    try:
        s = html[m.end():].decode("utf-8", errors="ignore")
        obj, _ = json.JSONDecoder().raw_decode(s)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def fetch_video_metadata(
    video_id: str,
    timeout: float = 5.0,
) -> Optional[Dict[str, Any]]:
    """Fetch rich per-video metadata from the watch page.

    Returns a dict with whichever of these fields could be parsed:
        description (str, full)
        viewCount (int, exact)
        tags (list[str])
        uploadDate (ISO date string, exact)
        durationSec (int)
        thumbnailUrl (str, max-res)
        category (str)
        likeCount (int)

    Returns None if the page fetch failed entirely.
    """
    if not video_id:
        return None
    req = Request(
        _english(f"https://www.youtube.com/watch?v={video_id}"),
        headers=_DEFAULT_HEADERS,
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            html = resp.read()
    except (URLError, TimeoutError, ConnectionError, OSError):
        return None
    except Exception:
        return None

    pr = _extract_yt_initial_player_response(html)
    if pr is None:
        return None

    vd = pr.get("videoDetails") or {}
    mf_outer = pr.get("microformat") or {}
    mf = mf_outer.get("playerMicroformatRenderer") or {}

    # Description: prefer videoDetails.shortDescription (always present, plain text).
    description = vd.get("shortDescription") or ""
    if not description:
        # Fallback to microformat.
        d = mf.get("description")
        if isinstance(d, dict):
            description = d.get("simpleText") or ""

    # View count: exact int. videoDetails.viewCount is a string.
    view_count = 0
    vc = vd.get("viewCount") or mf.get("viewCount") or "0"
    try:
        view_count = int(vc)
    except (TypeError, ValueError):
        view_count = 0

    # Tags
    tags = vd.get("keywords") or []
    if not isinstance(tags, list):
        tags = []
    tags = [t for t in tags if isinstance(t, str)]

    # Upload date: ISO from microformat (exact).
    upload_iso = ""
    raw_upload = mf.get("publishDate") or mf.get("uploadDate") or ""
    if isinstance(raw_upload, str) and raw_upload:
        # Trim to YYYY-MM-DD for consistency with our existing storage.
        upload_iso = raw_upload[:10]

    # Duration (exact)
    duration_sec = 0
    try:
        duration_sec = int(vd.get("lengthSeconds") or mf.get("lengthSeconds") or 0)
    except (TypeError, ValueError):
        duration_sec = 0

    # Largest thumbnail
    thumbnail_url = ""
    thumb = vd.get("thumbnail") or {}
    if isinstance(thumb, dict):
        thumbnail_url = _pick_largest_thumbnail(thumb.get("thumbnails"))
    if not thumbnail_url:
        thumb = mf.get("thumbnail") or {}
        if isinstance(thumb, dict):
            thumbnail_url = _pick_largest_thumbnail(thumb.get("thumbnails"))

    category = mf.get("category") or ""

    like_count = 0
    try:
        like_count = int(mf.get("likeCount") or 0)
    except (TypeError, ValueError):
        like_count = 0

    return {
        "description": description,
        "viewCount": view_count,
        "tags": tags,
        "uploadDate": upload_iso,
        "durationSec": duration_sec,
        "thumbnailUrl": thumbnail_url,
        "category": category,
        "likeCount": like_count,
    }


_IS_UNLISTED_RE = re.compile(r'"isUnlisted"\s*:\s*true')

# YouTube's anti-bot interstitial ("Sign in to confirm you're not a bot")
# arrives as playabilityStatus.status == "LOGIN_REQUIRED" - byte-for-byte the
# same status a genuinely private video returns. The reason prose is the only
# thing that separates them, and YouTube localises it: a probe from the
# production box came back as "Melde dich an, damit wir sehen, dass du kein
# Bot bist". So match the bot/robot token itself rather than any one phrasing.
# Latin scripts get a prefix-anchored word match; scripts without word
# boundaries are matched as plain substrings. Over-matching here is harmless -
# it costs us an "unknown", which is silent. Under-matching writes a lie.
_BOT_WORD_RE = re.compile(r"\b(?:bot|rob[oô])\w*", re.IGNORECASE)
_BOT_TOKENS = (
    "робот", "бот",          # ru / uk / bg
    "ボット",                  # ja
    "机器人", "機器人",         # zh
    "로봇", "봇",              # ko
    "روبوت",                  # ar
    "बॉट", "रोबोट",            # hi
    "บอท",                    # th
    "ρομπότ",                 # el
    "רובוט",                  # he
)

# Reasons that genuinely mean "the owner made this private". Same multilingual
# treatment, for the same reason: we must not read a German privacy notice as
# an unrecognised non-answer any more than the reverse.
#
# "priv" has to skip "privacy" and "privilege". YouTube's takedown prose for a
# third-party report is "This video is no longer available due to a privacy
# claim by a third party" - a REMOVAL, not a privacy change. Reading it as
# "private" would flip the row to private (immediately, undebounced), wipe its
# banked absence strikes, and then permanently hide it from the removal
# detector, which only ever evaluates rows it knows are public. The video is
# gone and we would never say so.
_PRIVATE_WORD_RE = re.compile(
    r"\b(?:priv(?!acy|ilege)\w*|pryw\w*|özel|gizli)\b", re.IGNORECASE
)
_PRIVATE_TOKENS = (
    "非公開", "非公开",         # ja / zh
    "비공개",                  # ko
    "私密",                    # zh
    "приватн", "частн",       # ru / uk
    "خاص",                    # ar
    "निजी",                    # hi
    "ส่วนตัว",                   # th
    "ιδιωτικ",                # el
)

# Verdicts. UNKNOWN is not a state of the video, it is a state of OUR
# knowledge: we looked and could not tell. Anything that acts on it as though
# it described the video is a bug.
VISIBILITY_PUBLIC = "public"
VISIBILITY_UNLISTED = "unlisted"
VISIBILITY_PRIVATE = "private"
VISIBILITY_MEMBERS = "members"
VISIBILITY_DELETED = "deleted"
VISIBILITY_UNKNOWN = "unknown"


class VisibilityProbe(NamedTuple):
    """One watch-page probe: what we concluded, plus the evidence for it."""

    verdict: str
    raw_status: str
    raw_reason: str

    @property
    def conclusive(self) -> bool:
        """True when ``verdict`` actually describes the video.

        Gate every write, snapshot and notification on this. A False here
        means the probe told us nothing, not that nothing changed.
        """
        return self.verdict != VISIBILITY_UNKNOWN


def _looks_like_bot_check(reason: str) -> bool:
    if not reason:
        return False
    if _BOT_WORD_RE.search(reason):
        return True
    return any(tok in reason for tok in _BOT_TOKENS)


def _looks_private(reason: str) -> bool:
    if not reason:
        return False
    if _PRIVATE_WORD_RE.search(reason):
        return True
    return any(tok in reason for tok in _PRIVATE_TOKENS)


def probe_video_visibility(
    video_id: str,
    timeout: float = 5.0,
) -> VisibilityProbe:
    """Ask YouTube's watch page what happened to a video.

    Always returns a VisibilityProbe - never None - because "we could not
    look" is a real answer this function has to give, and an Optional invites
    callers to drop it on the floor. ``verdict`` is one of:

        "public"   - still accessible and listed
        "unlisted" - page loads, video is unlisted
        "private"  - the owner made it private
        "members"  - members-only
        "deleted"  - gone; still a catch-all bucket, still needs debouncing
        "unknown"  - WE DO NOT KNOW. Not a verdict about the video.

    "unknown" covers the fetch failing, the page arriving without a
    playabilityStatus, and - the common case from our datacenter IP - YouTube
    serving its "confirm you're not a bot" interstitial, which it dresses up
    as LOGIN_REQUIRED exactly like a private video. A probe of ten
    unambiguously public videos from the production box came back bot-checked
    nine times, so this path is the norm, not an edge case.

    Callers MUST treat "unknown" as "change nothing, say nothing" (see
    VisibilityProbe.conclusive). Recording it as a privacy flip writes a
    change the creator never made into version history; letting it fall into
    a removal bucket eventually emails a user that a video they still have is
    gone. Prefer a false "unknown" over a false anything-else.

    ``raw_status`` / ``raw_reason`` are YouTube's verbatim playabilityStatus
    fields, kept only so we can diagnose misclassifications after the fact.
    They are localised, unversioned and often empty - never surface them to
    users and never let them drive a user-facing claim about WHY a video went
    away. Where we synthesize a status because YouTube gave us none, it is
    lower-cased with a leading underscore ("_fetch_failed") so it can never be
    confused with one of YouTube's own SCREAMING_CASE values.
    """
    if not video_id:
        return VisibilityProbe(VISIBILITY_UNKNOWN, "_no_video_id", "")
    req = Request(
        _english(f"https://www.youtube.com/watch?v={video_id}"),
        headers=_DEFAULT_HEADERS,
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="ignore")
    except (URLError, TimeoutError, ConnectionError, OSError):
        return VisibilityProbe(VISIBILITY_UNKNOWN, "_fetch_failed", "")
    except Exception:
        return VisibilityProbe(VISIBILITY_UNKNOWN, "_fetch_failed", "")

    status_m = _PLAYABILITY_STATUS_RE.search(text)
    if not status_m:
        # No playabilityStatus at all: a consent wall, a captcha page, or
        # markup we no longer understand. All of those are "we could not
        # look".
        return VisibilityProbe(VISIBILITY_UNKNOWN, "_no_playability_status", "")
    status = status_m.group(1)

    reason_m = _PLAYABILITY_REASON_RE.search(text)
    raw_reason = reason_m.group(1) if reason_m else ""
    reason = raw_reason.lower()

    if status == "OK":
        # Watch page loads - check if it's unlisted (still accessible, just
        # not listed publicly). Otherwise it's properly public.
        if _IS_UNLISTED_RE.search(text):
            return VisibilityProbe(VISIBILITY_UNLISTED, status, raw_reason)
        return VisibilityProbe(VISIBILITY_PUBLIC, status, raw_reason)

    # The bot wall can ride along on any non-OK status, so it is checked
    # before every verdict that would otherwise claim something about the
    # video.
    if _looks_like_bot_check(reason):
        return VisibilityProbe(VISIBILITY_UNKNOWN, status, raw_reason)

    # Members-only is the most specific of the real verdicts.
    if "members" in reason:
        return VisibilityProbe(VISIBILITY_MEMBERS, status, raw_reason)

    if status == "LOGIN_REQUIRED":
        # A genuinely private video returns LOGIN_REQUIRED with no
        # human-readable reason. When there IS prose and it does not say
        # "private", YouTube is telling us something else entirely - the bot
        # wall, an age gate, a purchase gate - and we have not established a
        # privacy change. Stay quiet.
        if not raw_reason.strip() or _looks_private(reason):
            return VisibilityProbe(VISIBILITY_PRIVATE, status, raw_reason)
        return VisibilityProbe(VISIBILITY_UNKNOWN, status, raw_reason)

    if _looks_private(reason):
        return VisibilityProbe(VISIBILITY_PRIVATE, status, raw_reason)

    # Catch-all: ERROR / UNPLAYABLE / etc - treat as deleted. This bucket is
    # broad on purpose (region blocks, copyright holds and rate-limit
    # interstitials all land here), which is exactly why callers debounce it
    # instead of acting on a single verdict.
    return VisibilityProbe(VISIBILITY_DELETED, status, raw_reason)


def fetch_video_visibility_detailed(
    video_id: str,
    timeout: float = 5.0,
) -> Optional[Tuple[str, str, str]]:
    """Back-compat shim over ``probe_video_visibility``. Prefer that instead.

    Returns ``(verdict, raw_status, raw_reason)``, or None when we could not
    establish anything. It never returns the "unknown" verdict, on purpose:
    callers written against this signature branch on the verdict string and
    funnel everything they do not recognise into their removal path, so
    handing them a new value would turn "we got bot-checked" into "your video
    was deleted". None is the one outcome they all already handle by leaving
    the video alone.

    That means the diagnostic pair is lost exactly when it would be most
    useful. Migrate to ``probe_video_visibility``, which keeps the evidence
    and makes the unknown state explicit.
    """
    probe = probe_video_visibility(video_id, timeout=timeout)
    if not probe.conclusive:
        return None
    return probe.verdict, probe.raw_status, probe.raw_reason


def fetch_video_visibility(
    video_id: str,
    timeout: float = 5.0,
) -> Optional[str]:
    """Determine why a video disappeared from a channel's /videos listing.

    YouTube's playabilityStatus.status values we care about:
        "OK"             - page loads normally; check microformat.isUnlisted
                           to distinguish unlisted from still-public
        "LOGIN_REQUIRED" - a private video, OR the anti-bot interstitial
                           wearing the same status; only the reason prose
                           tells them apart
        "ERROR"          - removed/terminated/etc; reason text disambiguates

    Returns one of:
        "public"   - still accessible and listed (we don't change anything)
        "unlisted" - page loads but video is unlisted
        "private"  - flipped to private
        "members"  - members-only
        "deleted"  - removed from YouTube
        None       - we could not establish anything (fetch failed, bot wall,
                     unparseable page); caller leaves the video alone

    Verdict-only wrapper kept for callers that don't record diagnostics.
    """
    detailed = fetch_video_visibility_detailed(video_id, timeout=timeout)
    return detailed[0] if detailed else None


def fetch_channel_about(
    handle_or_id: str,
    timeout: float = 5.0,
) -> Optional[Dict[str, Any]]:
    """Fetch the channel's About info from YouTube.

    Returns a dict with keys: name, description, country, joinedAt (ISO), links.
    Individual fields may be empty if YouTube didn't provide them.
    Returns None entirely if the page fetch failed.
    """
    url = _channel_url_for(handle_or_id, suffix="/about")
    if url is None:
        return None
    html = _fetch_html(url, timeout=timeout)
    if html is None:
        return None

    data = _extract_yt_initial_data(html)
    if data is None:
        return None

    metadata = _find_first(data, "channelMetadataRenderer") or {}
    about = _find_first(data, "aboutChannelViewModel") or {}

    name = metadata.get("title") or ""
    description = about.get("description") or metadata.get("description") or ""
    country = about.get("country") or ""

    joined_text = ""
    jd = about.get("joinedDateText")
    if isinstance(jd, dict):
        joined_text = jd.get("content", "") or ""
    joined_iso = _parse_joined_date(joined_text) or ""

    links: List[Dict[str, str]] = []
    raw_links = about.get("links") or []
    if isinstance(raw_links, list):
        for entry in raw_links:
            if isinstance(entry, dict):
                parsed = _parse_link(entry)
                if parsed:
                    links.append(parsed)

    return {
        "name": name,
        "description": description,
        "country": country,
        "joinedAt": joined_iso,
        "links": links,
    }


def fetch_channel_handle(
    handle_or_id: str,
    timeout: float = 5.0,
) -> Optional[str]:
    """The channel's canonical @handle, e.g. "@AFRFX".

    Read from channelMetadataRenderer.vanityChannelUrl, which is the
    address YouTube itself considers the channel's own.

    Deliberately NOT derived from the display name. The add-channel flow
    used to build the handle as "@" + name, which is a different string
    that happens to match often enough to look right: "AFRFX" gives
    "@AFRFX" correctly, while "Afraaz 🗿" gives "@Afraaz 🗿", which is
    not an address anybody can visit.

    Returns None on any failure - a channel really can have no handle,
    and the caller decides what to show instead.
    """
    url = _channel_url_for(handle_or_id)
    if url is None:
        return None
    html = _fetch_html(url, timeout=timeout)
    if html is None:
        return None
    data = _extract_yt_initial_data(html)
    if data is not None:
        meta = _find_first(data, "channelMetadataRenderer") or {}
        vanity = meta.get("vanityChannelUrl")
        if isinstance(vanity, str) and "/@" in vanity:
            tail = vanity.rsplit("/@", 1)[1].strip().strip("/")
            if tail:
                return f"@{tail}"
    # Same fallback shape as resolve_channel_id: pull it straight out of
    # the HTML when the parsed blob moves or changes name.
    m = re.search(rb'"vanityChannelUrl":"[^"]*?/@([\w.\-]+)"', html)
    if m:
        return "@" + m.group(1).decode("ascii", errors="ignore")
    return None
