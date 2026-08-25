# aether-b2-egress

Cloudflare **Worker** that fronts the private Backblaze B2 video bucket at
`dl.archive336.com` so large video downloads get free egress.

## Why

Storage moved to Backblaze B2 (Phase 1). B2 egress is free only up to 3x stored
bytes per month, then ~$0.01/GB. Cloudflare's Bandwidth Alliance makes the
B2 -> Cloudflare hop free with no cap, and Cloudflare -> client egress is free
too, so routing downloads through this Worker makes video egress free.

## How

The backend presigns a B2 GET URL (SigV4, path-style) and rewrites the host to
`dl.archive336.com` (backend env `STORAGE_PROXY_HOST`). The Worker
forwards the request to `s3.us-east-005.backblazeb2.com` with the path + query
preserved, so the signature (which signs `host`) stays valid, and streams the
response. It holds no credentials - all auth is in the short-lived presigned
query string.

The download flow routes through here: the video, plus (for the multi-file ZIP
download) its thumbnail + captions. The frontend fetch()es those cross-origin to
build the ZIP client-side, so they need the CORS headers this Worker adds (B2
has no CORS config of its own). UI `<img>` thumbnails and avatars stay direct on
B2: image display needs no CORS, and direct is faster and avoids extra Worker
hits.

If `STORAGE_PROXY_HOST` is unset on the backend, downloads fall back to direct
B2 URLs, so the backend is safe to deploy before this Worker exists.

## One-time deploy

Needs Cloudflare access to the ARCHIVE336 account (`CLOUDFLARE_ACCOUNT_ID_REDACTED`)
with Workers Scripts: Edit. That scope alone also attaches the `dl.` custom
domain (Zone DNS: Edit is not required - confirmed). The app's read-only API
token can't deploy Workers; use a scoped token.

```sh
cd workers/b2-egress-proxy

CLOUDFLARE_API_TOKEN=<scoped-token> \
CLOUDFLARE_ACCOUNT_ID=CLOUDFLARE_ACCOUNT_ID_REDACTED \
  npx wrangler deploy
```

The `custom_domain: true` route provisions `dl.archive336.com` (DNS +
cert) automatically. Then set `STORAGE_PROXY_HOST=dl.archive336.com` in
the backend `.env` and restart, and `.mp4` downloads route through here.

## Verify

```sh
# Mint a presigned URL on the prod box, host already rewritten to dl., then:
curl -sI -H 'Range: bytes=0-15' "https://dl.archive336.com/aether-archive-prod/<key>?<presigned-query>"
# Expect 206 Partial Content with Content-Type + Content-Range from B2.
```
