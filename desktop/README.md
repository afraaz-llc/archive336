# ARCHIVE336 — Desktop

The Tauri 2 desktop app that drains the sync queue from the ARCHIVE336 server.
Runs `yt-dlp` on the user's local machine (residential IP, the user's
browser cookies) so YouTube treats requests as a normal authenticated
viewer rather than a data-center bot.

## Architecture

```
React UI  ←events──   Rust worker (tokio)
   ↓ invoke              ↓ HTTP
[ ARCHIVE336 API ]      [ /sync-jobs/claim ]
                    [ yt-dlp subprocess ]
                    [ R2 PUT signed url ]
                    [ /sync-jobs/{id}/complete ]
```

`src-tauri/src/lib.rs` is the canonical reference for the protocol.
Differences here:
- Async (tokio) instead of threads
- State held in `tauri::State` and emitted to the React UI as
  `worker-status` events
- yt-dlp shells out via `tokio::process::Command` (assumes `yt-dlp` is
  on PATH; we'll bundle it as a Tauri sidecar in v2)

## Prerequisites

- Rust toolchain (`rustup` install)
- Node.js + npm
- `yt-dlp` on PATH:
  - macOS: `brew install yt-dlp`
  - Debian/Ubuntu: `sudo apt install yt-dlp` (or pip)
  - Windows: `winget install yt-dlp.yt-dlp`

## Develop

```bash
npm install
npm run tauri dev
```

This opens a window connected to the production API by default. Sign in
with your ARCHIVE336 credentials, pick the browser whose cookies yt-dlp
should read, hit Save then Start.

## Build a release binary

```bash
npm run tauri build
```

Outputs to `src-tauri/target/release/bundle/`:
- macOS: `.dmg` and `.app`
- Windows: `.msi`
- Linux: `.AppImage` + `.deb`

## What's still missing

This is v1. Known gaps for v2:
- Bundle yt-dlp as a Tauri sidecar so users don't need to install it
- Auto-update via GitHub Releases
- System tray icon (the app currently shows as a regular window)
- Better progress streaming (yt-dlp prints progress to stderr — we
  could parse that and emit per-second updates instead of jumping
  from 0% → 50% on download done)
- Cookie-from-browser fallback handling (Keychain prompt on macOS)
