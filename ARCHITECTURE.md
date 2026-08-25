# ARCHIVE336 — Architecture Map

If you're orienting in this codebase, **read this first**. It exists because the project has a non-obvious shape that's easy to get wrong after any context loss: there's a Python backend, a React web frontend, and a Tauri desktop app, and the desktop app — not the server-side worker — is what actually downloads videos in the MVP. That fact alone is worth dropping in here so it never gets lost again.

Companion doc:
- `README.md` — top-level intro.

---

## The three deployables

```
1.  Backend  (Python / FastAPI)              /opt/aether/ on Hetzner CPX11 (Ashburn)
2.  Web      (React + Vite static dist/)     Caddy on the same box, served at /
3.  Desktop  (Tauri = Rust + React)          User's own Mac, distributed as .app
```

All three talk to the same SQLite DB on the Hetzner box (via the FastAPI API). R2 stores video files and thumbnails. Cloudflare is the edge (DNS, CDN, SSL).

---

## CRITICAL: the desktop app is the real MVP worker

The MVP pivot, easy to lose during compaction, is "creators sync their *own* YouTube channels using their own logged-in Chrome session." The whole architecture follows from that.

```
                  Server-side worker.py             Desktop Tauri worker
                  ──────────────────────            ─────────────────────
  Runs on         Hetzner VM                        User's Mac
  Auth context    None (vanilla yt-dlp)             User's Chrome cookies
  Can download    Public videos only                Public + unlisted + own private
  Auth to API     Not authenticated                 Logged in as the user
  Status in MVP   Fallback / legacy                 PRIMARY
```

The server-side `backend/app/worker.py` exists and runs as a systemd service, but it can only pull public videos because it has no cookies and no OAuth tokens in its `ydl_opts`. **If you see a "this video is private" or 403 failure on a job, the desktop app needs to be the worker that picks it up, not the server-side fallback.** Smoke test passed May 21 2026: desktop app pulled a private 136 MB video end-to-end in ~65 seconds.

---

## Repo layout

```
aether-archive-tool/
├── backend/                  Python FastAPI server
│   └── app/
│       ├── routes/           HTTP route handlers (auth.py, youtube.py, billing.py, admin.py, errors.py)
│       ├── main.py           App init + Sentry + global exception handler
│       ├── db.py             SQLAlchemy engine + get_db dependency (SQLite)
│       ├── security.py       get_current_user / get_paid_user / get_admin_user / sessions
│       ├── models.py         All ORM models (User, UserSession, UserChannel, SyncJob, …)
│       ├── encryption.py     Fernet symmetric crypto for at-rest OAuth tokens
│       ├── google_oauth.py   OAuth flow + YouTube Data API + token revoke
│       ├── oauth_loader.py   load_user_credentials() — refresh + persist + soft-disconnect
│       ├── r2.py             Cloudflare R2 wrapper (upload, presign GET/PUT, stats)
│       ├── email.py          Resend SDK + inline-HTML templates
│       ├── billing.py        Stripe SDK + pricing math + invoice creation
│       ├── youtube_scrape.py HTML scraping fallback (avatar, channel stats)
│       ├── metadata_rescan.py Engine for the daily metadata rescan cron
│       ├── comments_rescan.py Engine for the daily comments rescan cron
│       └── worker.py         **PUBLIC-ONLY FALLBACK** sync worker (see CRITICAL section above)
├── src/                      React web frontend (Vite)
│   ├── main.tsx, App.tsx     Entry + router
│   ├── pages/                Home, YouTube, ChannelDetail, Settings, Admin, Dev, Auth, legal
│   ├── components/           Reusable UI (Paywalled, SyncPanel, VideoDetailPanel, …)
│   ├── lib/                  paymentStatus, pricing, errorReporter, types
│   └── auth/                 AuthContext (cookie-backed session state)
├── desktop/                  Tauri desktop app — THE REAL MVP WORKER
│   ├── src/                  React UI (Status / Auth / Settings tabs)
│   └── src-tauri/            Rust shell + worker loop
│       ├── src/lib.rs        Everything important: login, claim, yt-dlp invoke, R2 upload
│       └── tauri.conf.json   Window config, plugins
├── deploy/                   systemd units, Litestream config
├── ARCHITECTURE.md           This file
└── deploy.sh                 ./deploy.sh [all|backend|frontend]
```

---

## Backend (FastAPI)

### Auth model

Session-based, **not JWT**. Every login mints a 256-bit opaque token (`secrets.token_urlsafe(32)`) stored in `UserSession` with the user id and expiry. The token rides in an HttpOnly SameSite=Lax cookie. There's no refresh-token rotation; sessions live 30 days from mint and update `last_seen_at` on each authenticated call.

Three dependency layers in `backend/app/security.py`:
- `get_current_user` — 401 if no valid session
- `get_paid_user` — wraps the above + enforces `User.payment_status == 'active'`. **Returns 402 Payment Required, not 401**, because the frontend's fetch wrapper specifically catches 402 to route the user to `/settings#payment`. This is the single hook that gates write-side actions behind a payment method.
- `get_admin_user` — wraps `get_current_user` + checks `User.is_admin`, returns 403 otherwise.

`User.payment_status` is the canonical billing state. Possible values: `none` | `active` | `past_due` | `canceled`. Stripe webhooks are the source of truth (see "Billing pipeline" below).

### Database

SQLite at `/opt/aether/app/backend/aether.db`, replicated continuously to R2 by `litestream.service`. RPO is ~60 seconds. The recovery runbook is kept outside the repo.

Two SQLite gotchas the codebase handles for you:
1. Foreign-key enforcement is manually enabled on every connection in `db.py`. Without this, `ON DELETE CASCADE` is silently a no-op.
2. `check_same_thread=False` because the worker runs DB ops from threads.

Composite primary keys on multi-tenant tables: `(user_id, channel_id)` for UserChannel, `(user_id, channel_id, video_id)` for UserChannelVideo. Composite *foreign* keys are avoided because SQLite's story there is awkward — the codebase soft-FKs via `user_id` and lets cascades handle the rest.

JSON blobs are intentionally unschematized on the backend: `UserChannel.data_json`, `UserChannelVideo.data_json`, `UserYouTubeSettings.settings_json`. The frontend owns those schemas; the backend just round-trips them. You'll see `try: data = json.loads(...); except: data = {}` patterns scattered through.

### Background jobs (systemd timers in `deploy/`)

| Unit | Cadence | Tables touched | Why |
|---|---|---|---|
| `archive336-worker.service` | Daemon, polls every 5s | `sync_jobs`, `user_channel_videos`, R2 | **Public-only fallback worker.** Not the MVP path. |
| `archive336-meter.timer` | Daily 02:00 UTC | `usage_records` (insert) | Daily storage snapshot per user for billing |
| `archive336-bill.timer` | Monthly 3rd at 00:00 UTC | `usage_records` (update billed=true), `users` | Creates Stripe invoice when accrual ≥ $5 |
| `archive336-rescan-metadata.timer` | Daily 03:00 UTC | `video_field_snapshots`, `user_channel_videos`, R2 | Detect title/description/thumbnail/privacy changes |
| `archive336-rescan-comments.timer` | Daily 04:00 UTC | `video_comments`, `user_channel_videos` | Capture new comments, mark deleted ones, edit history |
| `litestream.service` | Continuous, 60s sync | (replicates aether.db to R2) | DB backup |

### Logging / errors

- Local: `logging.basicConfig` in `main.py` → systemd journal. Logger names are `aether.{module}` (`aether.auth`, `aether.worker`, etc.).
- Sentry: conditionally initialized if `SENTRY_DSN` is set, otherwise full no-op. `tracesSampleRate=0` to stay free-tier.
- Unhandled exceptions: caught by middleware in `main.py`, written to `error_log` table + forwarded to Sentry, return 500 JSON. DB write happens first so a Sentry outage doesn't lose the log.
- Client errors: posted to `POST /api/errors` (no auth required) → `error_log` row with `source='client'`. Visible in the `/dev` admin page.

### Email triggers (Resend)

All templates are inline HTML in `backend/app/email.py`. Wired triggers:
- Signup → verify email
- Forgot password → reset link
- Account deletion request → confirmation email (with optional JSON data export attached)
- Stripe `invoice.payment_failed` webhook → "your card failed"
- OAuth refresh failure → "your YouTube connection broke"

NOT wired yet (in ROADMAP post-MVP): invoice.paid receipt, video-deleted/channel-terminated/new-upload notifications, monthly digest. Templates exist as TBD; trigger sites don't.

### Billing pipeline (Stripe)

`backend/app/billing.py` + `backend/app/routes/billing.py`. Two separate tracks:
1. **Membership subscription** — $1/year per user, kicks off when they attach their first card via the SetupIntent flow. Stripe auto-renews. Activation flips `payment_status` to `active`.
2. **Storage usage** — pay-as-you-go, billed monthly on the 3rd via `archive336-bill.timer`. Accrual under $5 carries over; at account deletion, even small accruals get charged plus a $0.55 fee to cover Stripe's transaction cost.

Constants (single source of truth, exported by `GET /api/billing/prices`):
- $0.030/GB/month storage
- $0.020/GB downloads
- $5 minimum invoice threshold

Stripe API version is pinned to **`dahlia` (April 2026)**. The webhook endpoint at `/api/billing/webhook` verifies signatures with `STRIPE_WEBHOOK_SECRET` and handles: `setup_intent.succeeded`, `payment_method.attached`, `payment_method.detached`, `invoice.paid`, `invoice.payment_failed`, `customer.subscription.deleted`, `customer.deleted`.

### Worker orchestration endpoints (the desktop app uses these)

These live in `backend/app/routes/youtube.py` and are **unauthenticated** because the desktop client polls them as itself, not as a user:
- `GET /api/youtube/sync-jobs/active` — list pending/running jobs
- `GET /api/youtube/sync-jobs/claim` — atomically claim the next pending job (returns the job + a presigned R2 PUT URL)
- `POST /api/youtube/sync-jobs/{id}/heartbeat` — keep-alive at 50% progress
- `POST /api/youtube/sync-jobs/{id}/complete` — finalize with FileMeta (codecs, sha256, caption_languages…)
- `POST /api/youtube/sync-jobs/{id}/fail` — mark failed with last-2KB-of-stderr
- `POST /api/youtube/sync-jobs/{id}/caption-upload-url` — presigned URL for a specific caption language
- `POST /api/youtube/sync-jobs/retry-failed` — reset all `failed` jobs to `pending` (called by desktop app on startup)

`_reap_stale_claims()` in `routes/youtube.py` periodically resets jobs stuck in `running` for >2 minutes back to `pending` so a fresh worker can pick them up (crash recovery).

---

## Web frontend (React + Vite + Tailwind v4)

### Entry + routing

`src/main.tsx` initializes Sentry (only if `VITE_SENTRY_DSN` is set), installs the `/api/errors` reporter, then mounts `App.tsx`. `App.tsx` wraps everything in `AuthProvider` + `PricingProvider` and uses React Router 7 BrowserRouter.

Routes split into public (`/auth`, `/forgot-password`, `/verify-email`, `/terms`, `/privacy`) and protected (everything else, wrapped in `RequireAuth`). Protected routes render inside `AppShell` (sidebar + outlet).

| Route | Component | Hits |
|---|---|---|
| `/` | `pages/Home.tsx` | Dashboard. Connection status + channel list + archive stats |
| `/youtube` | `pages/YouTube.tsx` | Channel grid + AddChannelForm (paste-URL deliberately disabled — see Gotchas) + global archive settings |
| `/youtube/channel/:id` | `pages/ChannelDetail.tsx` | Video grid, SyncPanel, DownloadPanel, VideoDetailPanel |
| `/youtube/channel/:id/comments` | `pages/ChannelComments.tsx` | Deleted-comments feed |
| `/settings` | `pages/Settings.tsx` | Plan, YouTube OAuth, Notifications, Sessions, AccountEditor, DangerZone |
| `/admin` | `pages/Admin.tsx` | System metrics, user table, P&L (admin only) |
| `/dev` | `pages/Dev.tsx` | Error log viewer (admin only) |
| `/auth` | `pages/Auth.tsx` | Login + signup, hash-driven (`#login` / `#signup`) |

### Auth flow on the client

State machine in `src/auth/AuthContext.tsx`: `{ status: 'loading' | 'authed' | 'anon', user? }`. On mount, calls `/api/auth/me` to probe (cookie credentials are sent automatically). 2xx → `authed`, 401 → `anon`. There's NO token in localStorage — the session lives entirely in the HttpOnly cookie set by the backend. RequireAuth shows a blank loading screen during the probe to avoid a login-flash for already-authed users.

### Paywall enforcement on the client

The gate is `paymentStatus !== "active"`. State propagates via a deliberately weird pattern (no global Context for this):
1. `billing/PlanCard.tsx` fetches `/api/billing/status` and caches the result in `localStorage.aether_billing_status_cache`.
2. After writing the cache it dispatches a custom DOM event `archive336-billing-status-changed`.
3. `src/lib/paymentStatus.ts::readEffectivePaymentStatus()` reads the cache (plus an admin-only `aether_dev_payment_override` for UX testing).
4. Other components (Sidebar, Paywalled, etc.) listen for the custom event and re-render.

The `Paywalled` component wraps content with a blur + lock-icon overlay. As of May 2026 it's used on: Settings → Connected accounts, Settings → Notifications, Settings → Sessions. Other write-side flows (Add Channel, Sync) catch 402 from the backend and toast "add a payment method" instead.

### Shared types

`src/lib/types.ts` defines all client-side types (Video, Channel, ChannelArchiveSettings, FilterPreset, …). **These are NOT auto-generated from the backend.** The backend has its own Python types in `models.py` + `schemas.py`. If you change one, change both.

---

## Desktop app (Tauri)

### Process layout

Single 600×720px dark window. Rust shell (`desktop/src-tauri/src/lib.rs`) holds all business logic; React UI (`desktop/src/App.tsx`) is a thin status-display layer. They talk via Tauri IPC — the React side calls `invoke('command_name', { args })` and the Rust side handles it.

Tauri commands exposed to JS (see the bottom of `lib.rs`):
- `start_worker`, `stop_worker`, `get_status`
- `save_credentials`, `signin_now`, `signout_now`
- `verify_cookies`, `ytdlp_check`

### Worker loop

1. **Login** — `POST /api/auth/login` with stored creds. `reqwest` client has a cookie jar enabled, so the session cookie sticks for the rest of the session.
2. **Auto-retry** — `POST /api/youtube/sync-jobs/retry-failed` so any jobs left in `failed` from a previous crash get re-enqueued automatically.
3. **Cookie export** — `acquire_cookies_if_any()` walks the configured account slots and calls `acquire_cookies_via_webview()` on each, reading the embedded webview's own cookie store and writing a Netscape-format file that yt-dlp reads via `--cookies`. Nothing touches the user's daily browser: each account slot is a separate webview data store, so signing into a second YouTube account never disturbs the first, and no Keychain prompt is involved.
4. **Polling loop** — every 10s, `GET /api/youtube/sync-jobs/claim`. If a job comes back, process it; otherwise idle.
5. **Process a job:**
   a. Spawn yt-dlp with `--extractor-args youtube:player_client=default,web_creator,mweb` so it tries each client in order (default → Studio → mobile fallback).
   b. Format: `bv*[ext=mp4][height<=1080]+ba[ext=m4a]/best[…]`, merged to mp4.
   c. Captions: `--write-subs --sub-langs all --sub-format vtt --convert-subs vtt` — manual captions only, no auto-generated.
   d. Halfway through, `POST /api/youtube/sync-jobs/{id}/heartbeat` (one shot, not continuous).
   e. Probe the file with `ffprobe` (best-effort, all fields optional); SHA-256 the file.
   f. Upload to the presigned R2 URL (30-min timeout for slow uplinks).
   g. For each `.vtt` caption file, request a per-language presigned URL (`POST /sync-jobs/{id}/caption-upload-url`) and upload.
   h. `POST /api/youtube/sync-jobs/{id}/complete` with FileMeta (codecs, bitrates, fps, resolution, sha256, caption_languages).
   i. On any error, `POST /api/youtube/sync-jobs/{id}/fail` with the last 2 KB of stderr.

### Cookies come from our own webview, not the user's Chrome

Earlier builds read the user's Chrome cookie store directly, which only
ever saw the "Default" profile - anyone signed into YouTube in Profile 1,
2 or 3 got nothing usable. That path is gone. The app now signs the user
in through an embedded webview and keeps a separate cookie jar per
account slot, so multiple YouTube accounts coexist and no assumption is
made about the user's browser at all.

### Config persistence

`StoredConfig` lives in `~/Library/Application Support/com.aether.archivetool/config.json` with mode 0o600. Fields: `base_url`, `username`, `password`, `accounts` (one webview cookie jar per YouTube account), `autostart_declined`, `proven_channels`, `linked_channels`, and `channel_page_ids` - the delegated-identity page id per channel, which is what makes a brand channel's private videos reachable at all.

### Launching

Development: `! run-desktop.command` on the user's Desktop. The script `cd`s into `desktop/` and runs `npm run tauri dev`, which spins up Vite on :1420 + a Tauri webview pointed at it.

Production build is `cd desktop && npm run tauri build --target universal-apple-darwin` → `.app` + `.dmg` under `desktop/src-tauri/target/universal-apple-darwin/release/bundle/`. Use the `universal` target, not the default: a plain `npm run tauri build` produces an **arm64-only** binary, which silently breaks Intel Macs even though the website's download card advertises "Apple Silicon & Intel".

If a build fails with a path that includes `/Users/frog/Desktop/ARCHIVE336/...`, the Cargo `target/` directory has stale absolute paths from when the repo lived on the Desktop. Fix: `cd desktop/src-tauri && cargo clean`.

### Distribution (installer hosting)

Installers are served from `/opt/aether/downloads/` on the Hetzner box via a dedicated Caddy route:

```
handle /downloads/* {
    root * /opt/aether
    file_server browse
}
```

**They must NOT live in `/opt/aether/dist/`.** `deploy.sh frontend` does `rsync -az --delete dist/ → /opt/aether/dist/`, so anything in `dist/` that isn't in the local Vite build output gets deleted on the next frontend deploy. The separate directory is what keeps installers alive across deploys.

Upload a new build with a versioned filename and repoint the stable symlink, so the URL baked into the frontend never changes:

```
scp ARCHIVE336_<ver>_universal.dmg root@<host>:/opt/aether/downloads/
ssh root@<host> 'cd /opt/aether/downloads && ln -sf ARCHIVE336_<ver>_universal.dmg ARCHIVE336.dmg'
```

### NOT SIGNED YET — this is why the download buttons say "Coming soon"

Builds are **ad-hoc signed only** (`Signature=adhoc`, `TeamIdentifier=not set`). `spctl -a` on a quarantined copy returns `rejected — source=no usable signature`, meaning anyone who downloads the `.dmg` in a browser is blocked by Gatekeeper on first launch.

`WORKER_DOWNLOADS` in `src/pages/Settings.tsx` therefore still has `url: null` for every OS, which renders an honest "Coming soon" instead of a button that hands users an app macOS refuses to open. Flipping macOS live is a one-line change (`url: "/downloads/ARCHIVE336.dmg"`) once signing exists.

Real signing needs an Apple Developer Program membership ($99/yr) → a "Developer ID Application" certificate → `notarytool` credentials. Windows has the same problem via SmartScreen and needs its own code-signing certificate. Neither is purchased yet.

---

## Data flow walkthroughs

### Sign up + add card

1. User submits `/auth` form → `POST /api/auth/signup` → backend creates User, mints `EmailVerificationToken`, sends email, creates UserSession, sets cookie.
2. Frontend's `AuthContext` flips to `authed`.
3. User clicks Settings → PlanCard → "Add a payment method" → `POST /api/billing/setup-intent` → backend ensures Stripe Customer exists, returns `client_secret`.
4. Frontend mounts Stripe Elements with that secret; card details never touch our server.
5. On confirm, `POST /api/billing/setup-confirm` → backend verifies card is attached, creates the $1/yr membership Subscription, flips `payment_status='active'` synchronously.
6. Stripe also fires `setup_intent.succeeded` + `invoice.paid` webhooks to `/api/billing/webhook` which redundantly confirm `active`.

### Connect YouTube channel

1. Settings → "Connect your Google account" → full-page navigation to `/api/auth/youtube/start`.
2. Backend generates PKCE verifier + state, sets short-lived cookies, redirects user to Google's consent screen with `prompt=consent select_account`.
3. User grants → Google redirects to `/api/auth/youtube/callback?code=...&state=...`.
4. Backend validates state + verifier, exchanges code for access + refresh tokens, fetches userinfo + the user's YouTube channel, encrypts both tokens with Fernet, upserts `UserGoogleConnection`, redirects back to `/settings?yt_connect=ok`.
5. Settings page polls `/api/auth/youtube/status` → connected-account card appears with "Import channel" button.
6. Click Import → `POST /api/youtube/connected/import?google_user_id=...` → backend uses the stored OAuth creds to call YouTube Data API (channels.list, playlistItems.list, videos.list), upserts UserChannel + UserChannelVideo rows, archives the channel avatar + every video thumbnail to R2.

### Desktop worker pulls a video

1. User launches the desktop app, hits Start.
2. Worker logs in, probes cookies, retries failed jobs, extracts Chrome cookies to disk.
3. Polls `GET /api/youtube/sync-jobs/claim`. Backend looks for the next `pending` job, atomically flips it to `running`, returns the job + a presigned R2 PUT URL.
4. Worker runs yt-dlp with the OAuth-equivalent cookies file. yt-dlp tries `default` extractor → fails on private video → falls through to `web_creator` (Studio) → succeeds because the cookies file has the SAPISID/PSID set the Studio client needs.
5. Worker ffprobes + sha256s the mp4, uploads to R2, uploads each caption file via per-language presigned URL.
6. `POST /sync-jobs/{id}/complete` with FileMeta. Backend updates SyncJob to `done`, flips the UserChannelVideo's `data_json.status` to `archived` with the R2 key.
7. Web frontend's ChannelDetail page polls `/api/youtube/sync-jobs/active` every 3s and re-renders the video card as archived.

### Disconnect YouTube

1. Settings → "Disconnect" → `POST /api/auth/youtube/disconnect/{google_user_id}`.
2. Backend decrypts refresh token, calls `google_oauth.revoke_token()` → `POST https://oauth2.googleapis.com/revoke` (best-effort; failures logged, don't block).
3. Soft-deletes all UserChannel rows tied to that google_user_id (sets `removed_at = now`).
4. Hard-deletes the UserGoogleConnection row.
5. Next time the user clicks Connect, Google shows the full consent screen because the grant is gone on their side too. (Before May 21 2026 we didn't revoke at Google, so reconnects were silent re-grants — that's now fixed.)

---

## Gotchas / things easy to miss

1. **`backend/app/worker.py` is NOT the MVP worker.** It has no cookies and no OAuth tokens; it only works for public videos. The desktop Tauri app is the real worker. A docstring at the top of worker.py now spells this out so this never gets lost again.

2. **Worker orchestration endpoints are unauthenticated.** `/api/youtube/sync-jobs/{active,claim,heartbeat,complete,fail,caption-upload-url}` accept any caller. The job rows are filtered by user_id when serialized; never expose raw rows.

3. **Stripe API version is pinned to `dahlia` (April 2026).** If the dashboard auto-updates to a newer version, webhook payloads can break silently. There's already one fix for this (`Fix Stripe webhook customer extraction for dahlia API version` commit) — if it breaks again, that commit shows the shape.

4. **Payment status comes from Stripe webhooks**, not from polling. The synchronous `/setup-confirm` flips it locally for UX, but the source of truth is the webhook. If you see drift, re-sync from Stripe.

5. **Composite-PK JSON blobs are frontend-owned.** `UserChannel.data_json`, `UserChannelVideo.data_json`, `UserYouTubeSettings.settings_json` — the backend never validates these. Add a field on the frontend, it just works.

6. **Comments are never hard-deleted.** `VideoComment.deleted_at` is set when YouTube removes the comment, but the row stays forever. The whole point of the comments pipeline is the "recently deleted" feed.

7. **Soft-deleted UserChannel rows get revived on re-import.** The import endpoint detects `removed_at != NULL` and clears it instead of inserting a duplicate. Same channel id, same archived data, no purge cron interference.

8. **`AddChannelForm` paste-URL is deliberately disabled** (`src/components/AddChannelForm.tsx`). The parser still exists; only the UI is gated. Re-enable by dropping `disabled` + the "Coming soon" badge once we have ToS clearance for non-owned-channel archiving.

9. **Cache versioning by key suffix.** When a response shape changes, bump the localStorage key suffix (e.g. `aether_yt_status_cache_v2`) so old clients don't crash on stale-shape reads.

10. **Sentry frontend DSN ships in the public JS bundle** by design — Sentry DSNs are designed to be public identifiers. Don't panic when you see it in `VITE_SENTRY_DSN`.

11. **Frontend session token doesn't exist as such.** No JWT, no localStorage. Just the HttpOnly cookie set by the backend. `credentials: 'include'` on every fetch.

12. **`payment_status` propagation uses custom DOM events**, not Context. `PlanCard` writes to localStorage and dispatches `archive336-billing-status-changed`; everything else listens.

13. **Desktop app's hostname-in-User-Agent.** The desktop client identifies itself as `ARCHIVE336-Archive-Tool-Desktop/0.1.0 (Afraazs-MacBook-Pro)` so the Sessions panel can label which Mac uploaded which video.

14. **Rookie reads Chrome's "Default" profile only.** Logged in ROADMAP "Known limitations." Fix path written; not done yet.

15. **Two billing tracks.** Membership ($1/yr, auto-renews) and storage (monthly, $5 threshold). They're independent. Membership keeps `payment_status='active'` even if storage hasn't accrued anything yet.

16. **The frontend has a `mockChannels` array but it's empty.** No simulation code runs in production; the array was for early UI iteration.
