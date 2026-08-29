"""Transactional email - Resend SDK wrapper.

Wraps Resend's Python client behind one function per email type so the
calling code never touches the SDK or the HTML template directly.

All sends use the FROM address in RESEND_FROM_EMAIL (typically
noreply@archive336.com). Returns silently on success; raises
on Resend API errors so the caller decides whether to surface the
error or log-and-swallow.
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Optional

import resend


log = logging.getLogger("archive336.email")


def _configure() -> None:
    key = os.environ.get("RESEND_API_KEY")
    if not key:
        raise RuntimeError("RESEND_API_KEY missing from .env")
    resend.api_key = key


def _from() -> str:
    return os.environ.get("RESEND_FROM_EMAIL", "noreply@archive336.com")


def send_email_verification(to_email: str, verify_url: str) -> None:
    """Send a verification link asking the user to confirm they own
    this email address.

    Sent automatically on signup, and on demand when the user clicks
    "Verify" in the Account section of Settings.
    """
    _configure()

    html = f"""\
<!DOCTYPE html>
<html>
  <head><meta charset="utf-8"></head>
  <body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#111;background:#fff;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;margin:0 auto;padding:40px 24px;text-align:center;">
      <tr><td align="center">
        <p style="margin:0 0 32px;">
          <img src="https://archive336.com/logos/logo-light.svg"
               alt="ARCHIVE336"
               width="56" height="56"
               style="display:block;margin:0 auto;border:0;outline:none;text-decoration:none;">
        </p>
        <h1 style="font-size:22px;font-weight:800;margin:0 0 32px;letter-spacing:-0.02em;">
          ARCHIVE336
        </h1>
        <p style="margin:0;">
          <a href="{verify_url}"
             style="display:inline-block;background:#000;color:#fff;padding:14px 28px;
                    text-decoration:none;font-weight:600;font-size:15px;">
            Verify email
          </a>
        </p>
      </td></tr>
    </table>
  </body>
</html>
"""

    text = (
        "ARCHIVE336\n\n"
        f"Verify your email: {verify_url}"
    )

    resend.Emails.send(
        {
            "from": _from(),
            "to": to_email,
            "subject": "ARCHIVE336 - Verify email",
            "html": html,
            "text": text,
        }
    )
    log.info("verification email sent to %s", to_email)


def send_password_reset(to_email: str, reset_url: str) -> None:
    """Send the one-time password reset link to a user's email.

    The reset_url already has the plaintext token baked in as a query
    string param - anyone with the URL can redeem it for the next hour.
    """
    _configure()

    html = f"""\
<!DOCTYPE html>
<html>
  <head><meta charset="utf-8"></head>
  <body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#111;background:#fff;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;margin:0 auto;padding:40px 24px;text-align:center;">
      <tr><td align="center">
        <p style="margin:0 0 32px;">
          <img src="https://archive336.com/logos/logo-light.svg"
               alt="ARCHIVE336"
               width="56" height="56"
               style="display:block;margin:0 auto;border:0;outline:none;text-decoration:none;">
        </p>
        <h1 style="font-size:22px;font-weight:800;margin:0 0 24px;letter-spacing:-0.02em;">
          Reset password
        </h1>
        <p style="margin:8px 0 32px;">
          <a href="{reset_url}"
             style="display:inline-block;background:#000;color:#fff;padding:14px 28px;
                    text-decoration:none;font-weight:600;font-size:15px;">
            RESET
          </a>
        </p>
        <p style="font-size:12px;color:#999;line-height:1.6;margin:0;">
          This link is valid for 1 hour.
        </p>
      </td></tr>
    </table>
  </body>
</html>
"""

    text = (
        "Reset password\n\n"
        f"Click to reset: {reset_url}\n\n"
        "This link is valid for 1 hour."
    )

    resend.Emails.send(
        {
            "from": _from(),
            "to": to_email,
            "subject": "ARCHIVE336 - Reset password",
            "html": html,
            "text": text,
        }
    )
    log.info("password reset email sent to %s", to_email)


def send_account_deletion_confirmation(
    to_email: str,
    confirm_url: str,
    export_json_bytes: Optional[bytes] = None,
) -> None:
    """Verification email for the delete flow. By the time this lands,
    the user's card has already been charged in the dialog - clicking
    the link in this email is what finally wipes the account.
    Optionally attaches the user's data export as a JSON file
    (only when they ticked the box in the delete dialog).
    """
    _configure()

    attachment_note = (
        "<p style=\"font-size:13px;color:#666;line-height:1.6;margin:0 0 16px;\">"
        "Your account data export is attached to this email.</p>"
        if export_json_bytes
        else ""
    )

    html = f"""\
<!DOCTYPE html>
<html>
  <head><meta charset="utf-8"></head>
  <body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#111;background:#fff;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;margin:0 auto;padding:40px 24px;text-align:center;">
      <tr><td align="center">
        <p style="margin:0 0 32px;">
          <img src="https://archive336.com/logos/logo-light.svg"
               alt="ARCHIVE336"
               width="56" height="56"
               style="display:block;margin:0 auto;border:0;outline:none;text-decoration:none;">
        </p>
        <h1 style="font-size:22px;font-weight:800;margin:0 0 24px;letter-spacing:-0.02em;">
          Confirm account deletion
        </h1>
        {attachment_note}
        <p style="margin:8px 0 32px;">
          <a href="{confirm_url}"
             style="display:inline-block;background:#000;color:#fff;padding:14px 28px;
                    text-decoration:none;font-weight:600;font-size:15px;">
            DELETE
          </a>
        </p>
        <p style="font-size:12px;color:#999;line-height:1.6;margin:0;">
          This link is valid for 1 hour.
        </p>
      </td></tr>
    </table>
  </body>
</html>
"""

    text = (
        "Confirm account deletion\n\n"
        + (
            "Your account data export is attached to this email.\n\n"
            if export_json_bytes
            else ""
        )
        + f"Click to confirm: {confirm_url}\n\n"
        + "This link is valid for 1 hour."
    )

    payload: dict = {
        "from": _from(),
        "to": to_email,
        "subject": "ARCHIVE336 - Confirm account deletion",
        "html": html,
        "text": text,
    }
    if export_json_bytes:
        payload["attachments"] = [
            {
                "filename": "archive336-export.json",
                "content": base64.b64encode(export_json_bytes).decode("ascii"),
            }
        ]

    resend.Emails.send(payload)
    log.info("account deletion confirmation sent to %s", to_email)


def send_payment_failed(to_email: str, settings_url: str) -> None:
    """Notify the user that their payment method failed.

    Triggered by Stripe's `invoice.payment_failed` webhook. Stripe also
    emails the customer directly via Dashboard settings; this version
    lands them in our app where they can update the card in one click.
    """
    _configure()

    html = f"""\
<!DOCTYPE html>
<html>
  <head><meta charset="utf-8"></head>
  <body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#111;background:#fff;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;margin:0 auto;padding:40px 24px;text-align:center;">
      <tr><td align="center">
        <p style="margin:0 0 32px;">
          <img src="https://archive336.com/logos/logo-light.svg"
               alt="ARCHIVE336"
               width="56" height="56"
               style="display:block;margin:0 auto;border:0;outline:none;text-decoration:none;">
        </p>
        <h1 style="font-size:22px;font-weight:800;margin:0 0 24px;letter-spacing:-0.02em;">
          Payment method failed
        </h1>
        <p style="margin:8px 0 0;">
          <a href="{settings_url}"
             style="display:inline-block;background:#000;color:#fff;padding:14px 28px;
                    text-decoration:none;font-weight:600;font-size:15px;">
            UPDATE
          </a>
        </p>
      </td></tr>
    </table>
  </body>
</html>
"""

    text = (
        "Payment method failed\n\n"
        f"Update payment method: {settings_url}"
    )

    resend.Emails.send(
        {
            "from": _from(),
            "to": to_email,
            "subject": "ARCHIVE336 - Payment method failed",
            "html": html,
            "text": text,
        }
    )
    log.info("payment failed email sent to %s", to_email)


def send_oauth_disconnected(to_email: str, settings_url: str) -> None:
    """Notify the user that their Google/YouTube OAuth was revoked.

    Triggered the first time a refresh_token exchange fails with
    invalid_grant (or similar) - typically because the user revoked
    our access in their Google account settings, or because Google
    rotated the refresh token. The archive itself is unaffected;
    we just need them to reconnect to resume sync.
    """
    _configure()

    html = f"""\
<!DOCTYPE html>
<html>
  <head><meta charset="utf-8"></head>
  <body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#111;background:#fff;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;margin:0 auto;padding:40px 24px;text-align:center;">
      <tr><td align="center">
        <p style="margin:0 0 32px;">
          <img src="https://archive336.com/logos/logo-light.svg"
               alt="ARCHIVE336"
               width="56" height="56"
               style="display:block;margin:0 auto;border:0;outline:none;text-decoration:none;">
        </p>
        <h1 style="font-size:22px;font-weight:800;margin:0 0 24px;letter-spacing:-0.02em;">
          YouTube channel disconnected
        </h1>
        <p style="margin:8px 0 0;">
          <a href="{settings_url}"
             style="display:inline-block;background:#000;color:#fff;padding:14px 28px;
                    text-decoration:none;font-weight:600;font-size:15px;">
            RECONNECT
          </a>
        </p>
      </td></tr>
    </table>
  </body>
</html>
"""

    text = (
        "YouTube channel disconnected\n\n"
        f"Reconnect: {settings_url}"
    )

    resend.Emails.send(
        {
            "from": _from(),
            "to": to_email,
            "subject": "ARCHIVE336 - YouTube channel disconnected",
            "html": html,
            "text": text,
        }
    )
    log.info("oauth disconnected email sent to %s", to_email)


# Buttons that carry user content (a channel name, a video title) can be
# handed something arbitrarily long. Past this the button wraps into a black
# slab that stops reading as a button, so the label is clipped on a word
# boundary where possible. The full name still lives on the page the button
# opens.
_CTA_MAX_CHARS = 42


def _cta_label(text: str, fallback: str) -> str:
    label = (text or "").strip()
    if not label:
        return fallback
    if len(label) <= _CTA_MAX_CHARS:
        return label
    clipped = label[: _CTA_MAX_CHARS - 1].rstrip()
    # Prefer cutting at a space so we do not end mid-word, but only if that
    # still leaves a recognisable amount of the title.
    space = clipped.rfind(" ")
    if space >= _CTA_MAX_CHARS // 2:
        clipped = clipped[:space]
    return clipped + "\u2026"


def _notification_html(heading: str, sub: str, url: str, cta: str) -> str:
    """Shared shell for the archive notification emails: centered, logo,
    one heading, one short line, one all-caps button. Matches the
    transactional style used above."""
    sub_html = (
        f"""<p style="margin:0 0 28px;font-size:15px;color:#444;">{sub}</p>"""
        if sub
        else ""
    )
    return f"""\
<!DOCTYPE html>
<html>
  <head><meta charset="utf-8"></head>
  <body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#111;background:#fff;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;margin:0 auto;padding:40px 24px;text-align:center;">
      <tr><td align="center">
        <p style="margin:0 0 32px;">
          <img src="https://archive336.com/logos/logo-light.svg"
               alt="ARCHIVE336"
               width="56" height="56"
               style="display:block;margin:0 auto;border:0;outline:none;text-decoration:none;">
        </p>
        <h1 style="font-size:22px;font-weight:800;margin:0 0 24px;letter-spacing:-0.02em;">
          {heading}
        </h1>
        {sub_html}
        <p style="margin:8px 0 0;">
          <a href="{url}"
             style="display:inline-block;background:#000;color:#fff;padding:14px 28px;
                    text-decoration:none;font-weight:600;font-size:15px;">
            {cta}
          </a>
        </p>
      </td></tr>
    </table>
  </body>
</html>
"""


def send_video_deleted(
    to_email: str,
    channel_name: str,
    count: int,
    url: str,
    video_title: Optional[str] = None,
) -> None:
    """Video(s) we track are no longer visible on YouTube.

    The copy states the observation and nothing more. Both detection
    paths throw the cause away: the OAuth sync only knows the video fell
    out of the API listing, and the scrape path's "deleted" verdict is
    the catch-all branch of fetch_video_visibility, so a takedown, a
    region block, an age gate and a terminated account all land in it.
    We cannot name the cause, and certainly not the actor.

    It also makes no promise about the archived copy: the count is every
    row that flipped status, including videos we only ever discovered and
    never downloaded, so "your copy is safe" would sometimes be a lie.
    """
    _configure()
    heading = (
        "A video is unavailable on YouTube"
        if count == 1
        else f"{count} videos are unavailable on YouTube"
    )
    # No body line: the heading says what happened and the button says which
    # video, so a line naming the channel added nothing. One removal puts the
    # video title on the button and the url deep-links to that video; several
    # in one sweep have no single video to name, so the button falls back to
    # the channel and the url to the channel page.
    cta = _cta_label(video_title, channel_name) if count == 1 else channel_name
    cta = _cta_label(cta, "VIEW ARCHIVE")
    html = _notification_html(heading, "", url, cta)
    text = f"{heading}\n\n{cta}\n{url}"
    resend.Emails.send(
        {
            "from": _from(),
            "to": to_email,
            "subject": f"ARCHIVE336 - {heading}",
            "html": html,
            "text": text,
        }
    )
    log.info("video-deleted email sent to %s (%d)", to_email, count)


def send_channel_terminated(
    to_email: str, channel_name: str, url: str
) -> None:
    """We could not reach a tracked channel on YouTube.

    This fires after two consecutive failed scrapes of the same /about
    URL through the same fetcher, where every exception collapses to
    None. An HTTP error, a timeout, a bot interstitial and a markup
    change are indistinguishable from a real termination, so the copy
    asserts neither cause nor permanence.
    """
    _configure()
    heading = "A channel is unreachable on YouTube"
    # No body line: the heading says what happened and the button says which
    # channel, so a sentence restating both is just noise. The button carries
    # the name rather than a generic "VIEW ARCHIVE" so the mail is scannable
    # at a glance, and it is not CSS-uppercased - a channel name keeps its own
    # capitalisation and any emoji.
    html = _notification_html(heading, "", url, _cta_label(channel_name, "VIEW ARCHIVE"))
    text = f"{heading}\n\n{channel_name}\n{url}"
    resend.Emails.send(
        {
            "from": _from(),
            "to": to_email,
            "subject": f"ARCHIVE336 - {heading}",
            "html": html,
            "text": text,
        }
    )
    log.info("channel-terminated email sent to %s", to_email)


def send_new_upload(
    to_email: str, channel_name: str, video_title: str, url: str
) -> None:
    """A tracked channel published a new video (opt-in; off by default)."""
    _configure()
    heading = "New upload archived"
    # No body line and no generic "VIEW CHANNEL": the heading says what
    # happened, the button says which video, and the url opens that video.
    # channel_name stays the fallback for a video with no usable title.
    cta = _cta_label(video_title, _cta_label(channel_name, "VIEW ARCHIVE"))
    html = _notification_html(heading, "", url, cta)
    text = f"{heading}\n\n{cta}\n{url}"
    resend.Emails.send(
        {
            "from": _from(),
            "to": to_email,
            "subject": f"ARCHIVE336 - {heading}",
            "html": html,
            "text": text,
        }
    )
    log.info("new-upload email sent to %s", to_email)


def send_monthly_digest(
    to_email: str,
    *,
    archived: int,
    deletions_caught: int,
    storage_gb: float,
    url: str,
) -> None:
    """Monthly summary: what got archived, what went unavailable, and how
    much is stored (opt-in; off by default).

    The middle number is counted off deletedOnYoutubeAt, which is the same
    "we stopped being able to see it" signal send_video_deleted reports, so
    the copy says "went unavailable" rather than "deletions caught". The
    parameter keeps its old name because callers pass it by keyword.
    """
    _configure()
    heading = "Your monthly archive digest"
    sub = (
        f"{archived} archived &middot; {deletions_caught} went unavailable "
        f"&middot; {storage_gb:.1f} GB stored"
    )
    html = _notification_html(heading, sub, url, "VIEW ARCHIVE")
    text = (
        f"{heading}\n\n"
        f"{archived} archived, {deletions_caught} went unavailable, "
        f"{storage_gb:.1f} GB stored\n\nView archive: {url}"
    )
    resend.Emails.send(
        {
            "from": _from(),
            "to": to_email,
            "subject": f"ARCHIVE336 - {heading}",
            "html": html,
            "text": text,
        }
    )
    log.info("monthly digest email sent to %s", to_email)


def send_support_message_alert(
    *, username: str, kind: str, body: str, snapshot_text: str
) -> None:
    """Nudge the maintainer that a user has written in.

    Carries the account snapshot in the body, so most messages can be
    answered from the email itself without opening anything. The durable
    copy lives in the database; this is only the nudge.
    """
    _configure()
    from app.alerts import _admin_email

    to_admin = _admin_email()
    if not to_admin:
        return

    admin_url = "https://archive336.com/admin"
    esc = lambda t: t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    html = f"""\
<!DOCTYPE html>
<html>
  <head><meta charset="utf-8"></head>
  <body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#111;background:#fff;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;margin:0 auto;padding:40px 24px;">
      <tr><td>
        <h1 style="font-size:20px;font-weight:800;margin:0 0 20px;letter-spacing:-0.02em;">
          {esc(username)} sent a {esc(kind)}
        </h1>
        <p style="margin:0 0 24px;font-size:15px;line-height:1.6;white-space:pre-wrap;">{esc(body)}</p>
        <pre style="margin:0 0 24px;padding:16px;background:#f4f4f4;font-size:12px;line-height:1.5;white-space:pre-wrap;">{esc(snapshot_text)}</pre>
        <p style="margin:0;">
          <a href="{admin_url}"
             style="display:inline-block;background:#000;color:#fff;padding:14px 28px;
                    text-decoration:none;font-weight:600;font-size:15px;">
            REPLY
          </a>
        </p>
      </td></tr>
    </table>
  </body>
</html>
"""
    text = f"{username} sent a {kind}\n\n{body}\n\n{snapshot_text}\n\nReply: {admin_url}"

    resend.Emails.send(
        {
            "from": _from(),
            "to": to_admin,
            "subject": f"ARCHIVE336 - {kind} from {username}",
            "html": html,
            "text": text,
        }
    )


def send_support_reply(*, to_email: str, body: str) -> None:
    """The maintainer's reply, delivered as correspondence.

    No ticket number, no "your case has been updated". The product says
    one person maintains it; the email should read like that person
    wrote it, because they did.
    """
    _configure()

    support_url = "https://archive336.com/support"
    esc = lambda t: t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    html = f"""\
<!DOCTYPE html>
<html>
  <head><meta charset="utf-8"></head>
  <body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#111;background:#fff;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;margin:0 auto;padding:40px 24px;text-align:center;">
      <tr><td align="center">
        <p style="margin:0 0 32px;">
          <img src="https://archive336.com/logos/logo-light.svg"
               alt="ARCHIVE336"
               width="56" height="56"
               style="display:block;margin:0 auto;border:0;outline:none;text-decoration:none;">
        </p>
        <p style="margin:0 0 28px;font-size:15px;line-height:1.6;text-align:left;white-space:pre-wrap;">{esc(body)}</p>
        <p style="margin:8px 0 0;">
          <a href="{support_url}"
             style="display:inline-block;background:#000;color:#fff;padding:14px 28px;
                    text-decoration:none;font-weight:600;font-size:15px;">
            REPLY
          </a>
        </p>
      </td></tr>
    </table>
  </body>
</html>
"""
    text = f"{body}\n\nReply: {support_url}"

    resend.Emails.send(
        {
            "from": _from(),
            "to": to_email,
            "subject": "Re: your message to ARCHIVE336",
            "html": html,
            "text": text,
        }
    )


def send_queue_stalled_warning(
    to_email: str, summary: str, headline: str = "A backup queue has stalled"
) -> None:
    """Tell the operator a queue needs looking at.

    ``headline`` is passed rather than fixed because this same email
    carries two different findings. It said "A backup queue has
    stalled" for a run where no backup had stalled and no video job had
    failed at all - the whole storm was the nightly comment rescan
    hitting a stale YouTube session. An alert that misnames what is
    wrong is worse than no alert: it spends the operator's trust, and
    the next real stall reads like more of the same.

    Deliberately an operator email, not a customer one: the customer
    cannot act on "your queue is stalled" and the product's promise is
    that this is our job to notice. See app/queue_health.py for what
    counts as stalled and why it is not simply "an error happened".
    """
    _configure()

    admin_url = "https://archive336.com/admin"
    body = summary.replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")

    html = f"""\
<!DOCTYPE html>
<html>
  <head><meta charset="utf-8"></head>
  <body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#111;background:#fff;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;margin:0 auto;padding:40px 24px;text-align:center;">
      <tr><td align="center">
        <p style="margin:0 0 32px;">
          <img src="https://archive336.com/logos/logo-light.svg"
               alt="ARCHIVE336"
               width="56" height="56"
               style="display:block;margin:0 auto;border:0;outline:none;text-decoration:none;">
        </p>
        <h1 style="font-size:22px;font-weight:800;margin:0 0 24px;letter-spacing:-0.02em;">
          {headline}
        </h1>
        <p style="margin:0 0 28px;font-size:14px;line-height:1.6;color:#444;">
          {body}
        </p>
        <p style="margin:8px 0 0;">
          <a href="{admin_url}"
             style="display:inline-block;background:#000;color:#fff;padding:14px 28px;
                    text-decoration:none;font-weight:600;font-size:15px;">
            INVESTIGATE
          </a>
        </p>
      </td></tr>
    </table>
  </body>
</html>
"""

    text = f"{headline}\n\n{summary}\n\nInvestigate: {admin_url}"

    resend.Emails.send(
        {
            "from": _from(),
            "to": to_email,
            "subject": f"ARCHIVE336 - {headline[0].lower()}{headline[1:]}",
            "html": html,
            "text": text,
        }
    )


def send_sentry_quota_warning(to_email: str, events: int, cap: int) -> None:
    """Alert that Sentry's monthly free-tier event count is climbing.

    Fired once per calendar month by the polling _sentry_health() check
    in admin.py when events_this_month crosses the warning threshold.
    Dedup is file-marker based on the server. Sentry's free tier is
    5,000 errors/mo and silent-drops events when exceeded (i.e. we
    lose visibility into new errors), so an early warning matters more
    here than for billable services.
    """
    _configure()

    sentry_url = "https://sentry.io/organizations/archive336/issues/"

    html = f"""\
<!DOCTYPE html>
<html>
  <head><meta charset="utf-8"></head>
  <body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#111;background:#fff;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;margin:0 auto;padding:40px 24px;text-align:center;">
      <tr><td align="center">
        <p style="margin:0 0 32px;">
          <img src="https://archive336.com/logos/logo-light.svg"
               alt="ARCHIVE336"
               width="56" height="56"
               style="display:block;margin:0 auto;border:0;outline:none;text-decoration:none;">
        </p>
        <h1 style="font-size:22px;font-weight:800;margin:0 0 24px;letter-spacing:-0.02em;">
          Sentry {events:,}/{cap:,} this month
        </h1>
        <p style="margin:8px 0 0;">
          <a href="{sentry_url}"
             style="display:inline-block;background:#000;color:#fff;padding:14px 28px;
                    text-decoration:none;font-weight:600;font-size:15px;">
            INVESTIGATE
          </a>
        </p>
      </td></tr>
    </table>
  </body>
</html>
"""

    text = (
        f"Sentry {events:,}/{cap:,} this month\n\n"
        f"Investigate: {sentry_url}"
    )

    resend.Emails.send(
        {
            "from": _from(),
            "to": to_email,
            "subject": f"ARCHIVE336 - Sentry quota at {events:,}",
            "html": html,
            "text": text,
        }
    )
    log.info(
        "sentry quota warning email sent to %s (%d/%d)",
        to_email,
        events,
        cap,
    )


def send_hetzner_bandwidth_warning(
    to_email: str, used_bytes: int, included_bytes: int
) -> None:
    """Alert that Hetzner's billing-period outbound traffic is past the
    50% mark on the included cap.

    Fired by the daily archive336-hetzner-bandwidth cron when the live
    counter crosses the threshold. Above the included cap Hetzner
    bills ~$1.07/TB extra. Not catastrophic, but worth flagging so
    we can investigate before a leak or runaway user racks up real
    money.
    """
    _configure()

    used_gb = used_bytes / 1_000_000_000
    cap_gb = included_bytes / 1_000_000_000
    pct = (used_bytes / included_bytes * 100) if included_bytes else 0
    hetzner_url = "https://console.hetzner.com/projects/14375318/servers/128288947/graphs"

    html = f"""\
<!DOCTYPE html>
<html>
  <head><meta charset="utf-8"></head>
  <body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#111;background:#fff;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;margin:0 auto;padding:40px 24px;text-align:center;">
      <tr><td align="center">
        <p style="margin:0 0 32px;">
          <img src="https://archive336.com/logos/logo-light.svg"
               alt="ARCHIVE336"
               width="56" height="56"
               style="display:block;margin:0 auto;border:0;outline:none;text-decoration:none;">
        </p>
        <h1 style="font-size:22px;font-weight:800;margin:0 0 24px;letter-spacing:-0.02em;">
          Hetzner egress {pct:.0f}% ({used_gb:.0f}/{cap_gb:.0f} GB)
        </h1>
        <p style="margin:8px 0 0;">
          <a href="{hetzner_url}"
             style="display:inline-block;background:#000;color:#fff;padding:14px 28px;
                    text-decoration:none;font-weight:600;font-size:15px;">
            INVESTIGATE
          </a>
        </p>
      </td></tr>
    </table>
  </body>
</html>
"""

    text = (
        f"Hetzner egress {pct:.0f}% ({used_gb:.0f}/{cap_gb:.0f} GB) this period\n\n"
        f"Investigate: {hetzner_url}"
    )

    resend.Emails.send(
        {
            "from": _from(),
            "to": to_email,
            "subject": f"ARCHIVE336 - Hetzner egress {pct:.0f}%",
            "html": html,
            "text": text,
        }
    )
    log.info(
        "hetzner bandwidth warning email sent to %s (%.1f%% of cap)",
        to_email,
        pct,
    )


