# aether-dmarc-ingest

Cloudflare **Email Worker** that captures inbound DMARC aggregate reports
to R2 instead of forwarding them to a personal inbox.

## Why

`archive336.com` publishes a DMARC record with
`rua=mailto:dmarc@archive336.com`. Mailbox providers send daily
aggregate reports there. The domain's Cloudflare Email Routing has a
catch-all that forwards everything to a personal Gmail, so the reports
landed in the inbox. This worker intercepts `dmarc@` and writes each
report to R2 instead — off personal email, retained, and parseable on
demand by `backend/scripts/dmarc_summary.py`.

It stores the raw `.eml` (not the unzipped XML) to stay tiny; MIME + zip
+ XML parsing lives in the Python script.

## One-time deploy

Needs Cloudflare access with Workers + R2 + Email Routing edit (the app's
read-only API token can't deploy Workers). Authorize once with
`wrangler login`, or use an API token with those scopes.

```sh
cd workers/dmarc-ingest

# 1. Create the destination bucket (once).
wrangler r2 bucket create aether-dmarc-reports

# 2. Deploy the worker.
wrangler deploy

# 3. Route dmarc@ to it. Dashboard:
#    Email > Email Routing > Routing rules > Create
#      Custom address: dmarc@archive336.com
#      Action: Send to a Worker > aether-dmarc-ingest
#    (This explicit rule overrides the catch-all for dmarc@ only.)
#    Or via API (needs Email Routing Rules:Edit):
#      POST /zones/<zone_id>/email/routing/rules
#      { "matchers":[{"type":"literal","field":"to",
#                     "value":"dmarc@archive336.com"}],
#        "actions":[{"type":"worker","value":["aether-dmarc-ingest"]}],
#        "enabled": true, "name": "dmarc -> ingest worker" }
```

## Reading the reports

On the prod box (R2_* already in env):

```sh
set -a; . /opt/aether/.env; set +a
/opt/aether/venv/bin/python -m scripts.dmarc_summary            # latest 5
/opt/aether/venv/bin/python -m scripts.dmarc_summary --fails-only
```
