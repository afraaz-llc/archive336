<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="public/logos/logo.svg">
  <img src="public/logos/logo-light.svg" alt="ARCHIVE336" width="72" height="72">
</picture>

# ARCHIVE336

`// HOARD RESPONSIBLY`

</div>

---

## Architecture

Three processes, and the split is the point.

**`src/`** — the control panel. React, Vite, Tailwind. Renders state and issues
commands. Never touches YouTube.

**`desktop/`** — the worker. Tauri, Rust, with an embedded webview holding the
user's YouTube session. Runs `yt-dlp` on the user's own machine and uploads
straight to object storage. Downloads never pass through our infrastructure.

**`backend/`** — FastAPI and SQLite. Owns the job queue, the storage ledger and
billing. Hands out presigned URLs and records what came back. **Never holds a
YouTube session.**

That constraint drives most of the design. A server that authenticated to
YouTube on the user's behalf would be far simpler and would mean custody of
their account. It also would not work: YouTube treats data-centre IPs
differently from a signed-in browser, which is why enumeration and download
both happen client-side.

## Layout

```
src/                    website
backend/app/            API, billing, storage ledger
backend/scripts/        cron jobs — billing, rescans, tripwires
backend/tests/          pytest
desktop/src-tauri/      the worker (Rust)
workers/                Cloudflare Workers — egress proxy, DMARC ingest
```

`ARCHITECTURE.md` is the map: data model, job lifecycle, billing, and the
reasoning behind each. Start there.

## Licence

[PolyForm Shield 1.0.0](LICENSE)
