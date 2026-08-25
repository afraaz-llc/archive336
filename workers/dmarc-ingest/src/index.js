/**
 * DMARC aggregate report ingest - Cloudflare Email Worker.
 *
 * Bound to dmarc@archive336.com via an Email Routing rule. Every
 * inbound aggregate report (a small RFC822 message with a zipped/gzipped
 * XML attachment) is written verbatim to R2 under
 *
 *   dmarc/<YYYY>/<MM>/<DD>_<iso-ts>_<message-id>.eml
 *
 * and then the worker returns normally. It deliberately does NOT call
 * message.forward(...), so reports are captured but never delivered to a
 * mailbox - they stay out of personal email. Returning without rejecting
 * tells Email Routing the message was accepted, so senders see success.
 *
 * We store the raw .eml rather than the unzipped XML on purpose: MIME +
 * zip + XML parsing is far nicer in Python, done on demand by
 * backend/scripts/dmarc_summary.py. Keeping the worker tiny means there's
 * almost nothing here to break.
 */

function pad2(n) {
  return String(n).padStart(2, "0")
}

function sanitize(s) {
  return (s || "")
    .replace(/[<>]/g, "")
    .replace(/[^a-zA-Z0-9._-]/g, "_")
    .slice(0, 80)
}

export default {
  /**
   * @param {ForwardableEmailMessage} message
   * @param {{ DMARC_BUCKET: R2Bucket }} env
   */
  async email(message, env) {
    const raw = await new Response(message.raw).arrayBuffer()

    const now = new Date()
    const y = now.getUTCFullYear()
    const m = pad2(now.getUTCMonth() + 1)
    const d = pad2(now.getUTCDate())
    const ts = now.toISOString().replace(/[:.]/g, "-")
    const msgId = sanitize(message.headers.get("message-id")) || "report"
    const key = `dmarc/${y}/${m}/${d}_${ts}_${msgId}.eml`

    await env.DMARC_BUCKET.put(key, raw, {
      httpMetadata: { contentType: "message/rfc822" },
      customMetadata: {
        from: message.from || "",
        to: message.to || "",
        subject: message.headers.get("subject") || "",
      },
    })
    // No message.forward(...) - captured, not delivered.
  },
}
