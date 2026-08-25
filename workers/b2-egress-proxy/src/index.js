/**
 * Backblaze B2 egress proxy - Cloudflare Worker.
 *
 * Serves the private video bucket at dl.archive336.com. The backend
 * presigns a B2 GET URL (SigV4, path-style) and rewrites the host to this
 * Worker before handing it to the browser. The Worker forwards the request to
 * B2's S3 endpoint with the path + query preserved byte-for-byte, so the
 * signature (which signs `host`) stays valid, and streams the response back.
 *
 * Why: B2 -> Cloudflare egress is free under the Bandwidth Alliance and
 * Cloudflare -> client egress is free, so routing large video downloads through
 * here costs no egress. Thumbnails shown in the UI stay on B2 directly, but the
 * download/ZIP flow (video + thumbnail + captions) all routes through here so
 * the cross-origin fetch()es the frontend does for client-side zipping get the
 * CORS headers below from one place (B2 itself has no CORS config).
 *
 * This Worker holds no credentials - all auth is in the presigned query string,
 * which the backend mints short-lived (5 minutes). CORS is wide-open on purpose:
 * the presigned signature is the access control, not the Origin, and these
 * fetches carry no credentials, so Allow-Origin: * is safe.
 */
// COUPLED to the backend's STORAGE_ENDPOINT (backend/app/r2.py:132, set
// from secrets/b2.env). SigV4 signs the Host header, so the backend signs
// for the real B2 host, swaps the host to this Worker, and this Worker
// must put the identical host back or every signature fails. Change the
// bucket region or move providers WITHOUT redeploying this Worker and
// proxied downloads 403 while direct thumbnails keep working - a silent,
// partial breakage that looks like anything but a config mismatch.
//
// Read from a binding so the value is declared config in wrangler.jsonc
// rather than buried in source, and grep for STORAGE_ENDPOINT before
// changing either side.
const DEFAULT_B2_HOST = "s3.us-east-005.backblazeb2.com"

/** The B2 host to proxy to. Overridable via the B2_HOST var in
 *  wrangler.jsonc without touching this file. */
function b2Host(env) {
  return (env && env.B2_HOST) || DEFAULT_B2_HOST
}

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
  "Access-Control-Allow-Headers": "Range, If-None-Match, If-Modified-Since, If-Range",
  "Access-Control-Expose-Headers":
    "Content-Length, Content-Range, Content-Disposition, Content-Type, Accept-Ranges, ETag",
  "Access-Control-Max-Age": "86400",
}

function withCors(headers) {
  for (const [k, v] of Object.entries(CORS)) headers.set(k, v)
  return headers
}

export default {
  async fetch(request, env) {
    // CORS preflight (the simple GET fetch()es don't trigger one, but be safe).
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: withCors(new Headers()) })
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method Not Allowed", { status: 405, headers: withCors(new Headers()) })
    }
    const url = new URL(request.url)
    if (url.pathname === "/" || url.pathname === "") {
      return new Response("Not Found", { status: 404, headers: withCors(new Headers()) })
    }

    // Rebuild the original B2 URL: same path (/<bucket>/<key>) and query
    // (the X-Amz-* signature + response-* overrides). Only the host changes.
    const b2Url = "https://" + b2Host(env) + url.pathname + url.search

    // Forward only the headers that matter for ranged / conditional GETs (video
    // seeking, resume). Auth is in the query string, not headers, and we
    // deliberately do not pass the client's Host through.
    const fwd = new Headers()
    for (const h of ["Range", "If-None-Match", "If-Modified-Since", "If-Range"]) {
      const v = request.headers.get(h)
      if (v) fwd.set(h, v)
    }

    const resp = await fetch(b2Url, { method: request.method, headers: fwd })

    // Pass B2's response straight through: status (200 / 206 / 304 / 403 / 404)
    // and the Content-Type, Content-Disposition, Content-Length, Content-Range,
    // Accept-Ranges, ETag headers all come from B2. Drop Set-Cookie noise and
    // attach CORS so the frontend can read the bytes for client-side zipping.
    const headers = new Headers(resp.headers)
    headers.delete("Set-Cookie")
    withCors(headers)
    return new Response(resp.body, {
      status: resp.status,
      statusText: resp.statusText,
      headers,
    })
  },
}
