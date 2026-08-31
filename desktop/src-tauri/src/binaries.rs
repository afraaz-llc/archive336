// ---------------------------------------------------------------------------
// Managed external binaries (yt-dlp, ffmpeg, ffprobe)
//
// A normal user has none of these installed, so the worker can't assume the
// machine "has developer tools". On startup we download plain, statically
// linked builds into the app's own data directory and run those, falling back
// to anything already on PATH (convenient in dev).
//
//   - yt-dlp   : the downloader. It breaks whenever YouTube changes its
//                player, so we version-check it against its latest GitHub
//                release each launch and refresh it when it's behind.
//   - ffmpeg   : merges yt-dlp's separate 1080p video + audio streams. We
//                pin a stable release; it rarely needs updating.
//   - ffprobe  : optional metadata reader, from the same source as ffmpeg.
//                If it's ever missing the job still succeeds with less detail.
//
// Everything here is a plain single executable - no archives to unpack.
// ---------------------------------------------------------------------------

use std::path::{Path, PathBuf};
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager};

/// Pinned ffmpeg-static release (ffmpeg 6.1.1). Bump deliberately.
const FFMPEG_STATIC_TAG: &str = "b6.1.1";

/// Event the UI listens on for setup progress lines.
pub const PROGRESS_EVENT: &str = "binaries://progress";

struct Assets {
    ytdlp: &'static str,
    ffmpeg: &'static str,
    ffprobe: &'static str,
    /// Deno release zip. yt-dlp needs a JS runtime to solve YouTube's
    /// n-challenge; without one it quietly serves only low-res formats.
    deno: &'static str,
}

/// Asset file names for the platform/arch we were built for. None on a target
/// we don't ship managed builds for (the worker then relies purely on PATH).
fn assets_for_target() -> Option<Assets> {
    Some(match (std::env::consts::OS, std::env::consts::ARCH) {
        ("macos", "aarch64") => Assets {
            ytdlp: "yt-dlp_macos",
            ffmpeg: "ffmpeg-darwin-arm64",
            ffprobe: "ffprobe-darwin-arm64",
            deno: "deno-aarch64-apple-darwin.zip",
        },
        ("macos", "x86_64") => Assets {
            ytdlp: "yt-dlp_macos",
            ffmpeg: "ffmpeg-darwin-x64",
            ffprobe: "ffprobe-darwin-x64",
            deno: "deno-x86_64-apple-darwin.zip",
        },
        ("linux", "x86_64") => Assets {
            ytdlp: "yt-dlp_linux",
            ffmpeg: "ffmpeg-linux-x64",
            ffprobe: "ffprobe-linux-x64",
            deno: "deno-x86_64-unknown-linux-gnu.zip",
        },
        ("linux", "aarch64") => Assets {
            ytdlp: "yt-dlp_linux_aarch64",
            ffmpeg: "ffmpeg-linux-arm64",
            ffprobe: "ffprobe-linux-arm64",
            deno: "deno-aarch64-unknown-linux-gnu.zip",
        },
        ("windows", "x86_64") => Assets {
            ytdlp: "yt-dlp.exe",
            ffmpeg: "ffmpeg-win32-x64.exe",
            ffprobe: "ffprobe-win32-x64.exe",
            deno: "deno-x86_64-pc-windows-msvc.zip",
        },
        _ => return None,
    })
}

fn ytdlp_url(asset: &str) -> String {
    format!("https://github.com/yt-dlp/yt-dlp/releases/latest/download/{asset}")
}

fn deno_url(asset: &str) -> String {
    format!("https://github.com/denoland/deno/releases/latest/download/{asset}")
}

fn ffmpeg_static_url(asset: &str) -> String {
    format!("https://github.com/eugeneware/ffmpeg-static/releases/download/{FFMPEG_STATIC_TAG}/{asset}")
}

/// Local filename for a tool, with the platform's executable extension.
fn local_name(base: &str) -> String {
    if cfg!(windows) {
        format!("{base}.exe")
    } else {
        base.to_string()
    }
}

/// `<app data>/bin`, created if missing.
fn bin_dir(app: &AppHandle) -> Result<PathBuf, String> {
    let dir = app
        .path()
        .app_data_dir()
        .map_err(|e| e.to_string())?
        .join("bin");
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    Ok(dir)
}

/// Our managed copy of a tool, if it's been downloaded.
fn managed_path(app: &AppHandle, base: &str) -> Option<PathBuf> {
    let p = bin_dir(app).ok()?.join(local_name(base));
    p.is_file().then_some(p)
}

/// Resolve a tool by bare name ("yt-dlp", "ffmpeg", "ffprobe"): our managed
/// copy first, then whatever happens to be on PATH (dev convenience).
pub fn resolve(app: &AppHandle, base: &str) -> Option<PathBuf> {
    managed_path(app, base).or_else(|| which::which(base).ok())
}

fn http_client() -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        // GitHub's API rejects requests without a User-Agent.
        .user_agent(concat!("AetherArchiveWorker/", env!("CARGO_PKG_VERSION")))
        // Big static binaries on slow links; give them room.
        .timeout(Duration::from_secs(600))
        .build()
        .map_err(|e| e.to_string())
}

#[cfg(unix)]
fn mark_executable(p: &Path) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;
    let mut perms = std::fs::metadata(p).map_err(|e| e.to_string())?.permissions();
    perms.set_mode(0o755);
    std::fs::set_permissions(p, perms).map_err(|e| e.to_string())
}
#[cfg(not(unix))]
fn mark_executable(_p: &Path) -> Result<(), String> {
    Ok(())
}

/// Download `url` to `dest`, atomically (temp file + rename) and executable.
async fn download(client: &reqwest::Client, url: &str, dest: &Path) -> Result<(), String> {
    let resp = client.get(url).send().await.map_err(|e| e.to_string())?;
    if !resp.status().is_success() {
        return Err(format!("GET {url} -> HTTP {}", resp.status()));
    }
    let bytes = resp.bytes().await.map_err(|e| e.to_string())?;
    let tmp = dest.with_extension("part");
    std::fs::write(&tmp, &bytes).map_err(|e| e.to_string())?;
    mark_executable(&tmp)?;
    // Rename last, so a half-written file is never seen as "installed".
    std::fs::rename(&tmp, dest).map_err(|e| e.to_string())?;
    Ok(())
}

/// yt-dlp's latest release tag (its version string), if GitHub is reachable.
async fn latest_ytdlp_tag(client: &reqwest::Client) -> Option<String> {
    let v: serde_json::Value = client
        .get("https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest")
        .send()
        .await
        .ok()?
        .json()
        .await
        .ok()?;
    v.get("tag_name")?.as_str().map(str::to_string)
}

/// The installed yt-dlp's version, by running `--version`.
async fn installed_ytdlp_version(path: &Path) -> Option<String> {
    let out = tokio::process::Command::new(path)
        .arg("--version")
        .output()
        .await
        .ok()?;
    out.status
        .success()
        .then(|| String::from_utf8_lossy(&out.stdout).trim().to_string())
}

fn emit(app: &AppHandle, msg: &str) {
    let _ = app.emit(PROGRESS_EVENT, msg);
}

/// Make sure yt-dlp + ffmpeg (+ ffprobe) are present and yt-dlp is current.
/// Best-effort on the network: if we can't reach GitHub but copies already
/// exist, we keep using them rather than erroring.
/// Re-check yt-dlp against the latest release and replace it if it has
/// fallen behind. Returns the new version when it updated.
///
/// `ensure_all` already does this, but only at launch - and this app is
/// built to sit in the tray for weeks at a time. yt-dlp is the one tool
/// here with an adversarial upstream: YouTube changes something, the
/// extractor breaks, and every download starts failing with things like
/// "unable to extract yt initial data" until a new yt-dlp ships. A
/// worker that only updates when someone happens to restart it will sit
/// broken for exactly as long as it stays open, which for a background
/// backup app is the normal case rather than the unlucky one.
///
/// Deliberately quiet: no setup-progress events. Those belong to the
/// launch checklist that gates the Start button, and a routine
/// background refresh is not something to interrupt a running worker
/// with. Errors are logged and swallowed - failing to reach GitHub is
/// not a reason to stop backing up with the copy we already have.
pub async fn refresh_ytdlp_if_stale(app: &AppHandle) -> Option<String> {
    let assets = assets_for_target()?;
    let client = http_client().ok()?;
    let dir = bin_dir(app).ok()?;
    let path = dir.join(local_name("yt-dlp"));
    if !path.is_file() {
        // Never installed. That is ensure_all's job, not this one -
        // it knows how to tell the UI that setup is still running.
        return None;
    }

    let have = installed_ytdlp_version(&path).await?;
    let latest = latest_ytdlp_tag(&client).await?;
    if have == latest {
        return None;
    }

    log::info!("yt-dlp {have} is behind {latest}; updating");
    match download(&client, &ytdlp_url(assets.ytdlp), &path).await {
        Ok(()) => {
            log::info!("yt-dlp updated to {latest}");
            Some(latest)
        }
        Err(e) => {
            log::warn!("yt-dlp update to {latest} failed, keeping {have}: {e}");
            None
        }
    }
}


pub async fn ensure_all(app: &AppHandle) -> Result<(), String> {
    let assets = match assets_for_target() {
        Some(a) => a,
        None => {
            log::warn!("no managed binaries for this platform; relying on PATH");
            return Ok(());
        }
    };
    let client = http_client()?;
    let dir = bin_dir(app)?;

    // yt-dlp: install if missing, refresh if a newer release exists.
    let ytdlp = dir.join(local_name("yt-dlp"));
    let need_ytdlp = if ytdlp.is_file() {
        match (
            installed_ytdlp_version(&ytdlp).await,
            latest_ytdlp_tag(&client).await,
        ) {
            (Some(have), Some(latest)) => have != latest,
            _ => false, // offline / unknown -> leave the working copy alone
        }
    } else {
        true
    };
    if need_ytdlp {
        emit(app, "Setting up the downloader (yt-dlp)");
        download(&client, &ytdlp_url(assets.ytdlp), &ytdlp).await?;
    }

    // ffmpeg: required for 1080p merges. Pinned + stable, so just ensure present.
    let ffmpeg = dir.join(local_name("ffmpeg"));
    if !ffmpeg.is_file() {
        emit(app, "Setting up the video encoder (ffmpeg)");
        download(&client, &ffmpeg_static_url(assets.ffmpeg), &ffmpeg).await?;
    }

    // ffprobe: optional metadata reader; never fail the whole setup over it.
    let ffprobe = dir.join(local_name("ffprobe"));
    if !ffprobe.is_file() {
        emit(app, "Setting up the metadata reader (ffprobe)");
        if let Err(e) = download(&client, &ffmpeg_static_url(assets.ffprobe), &ffprobe).await {
            log::warn!("ffprobe download failed (metadata will be limited): {e}");
        }
    }

    // Deno: the JS runtime yt-dlp uses for YouTube's n-challenge. Without
    // it downloads still succeed but silently drop to ~360p, so a user who
    // asked for 1080p would quietly get something far worse. Optional in the
    // sense that we never fail setup over it.
    let deno = dir.join(local_name("deno"));
    if !deno.is_file() {
        emit(app, "Setting up the quality unlocker (deno)");
        if let Err(e) = download_deno(&client, &deno_url(assets.deno), &deno).await {
            log::warn!("deno download failed (downloads may be limited to low resolutions): {e}");
        }
    }

    emit(app, "ready");
    Ok(())
}

/// Download deno's release zip and extract just the executable. Everything
/// else we manage is a bare binary; deno is the one archive.
async fn download_deno(
    client: &reqwest::Client,
    url: &str,
    dest: &Path,
) -> Result<(), String> {
    let bytes = client
        .get(url)
        .send()
        .await
        .map_err(|e| format!("deno download: {e}"))?
        .error_for_status()
        .map_err(|e| format!("deno download: {e}"))?
        .bytes()
        .await
        .map_err(|e| format!("deno download body: {e}"))?;

    let reader = std::io::Cursor::new(bytes);
    let mut archive =
        zip::ZipArchive::new(reader).map_err(|e| format!("deno zip: {e}"))?;
    let wanted = local_name("deno");
    for i in 0..archive.len() {
        let mut f = archive.by_index(i).map_err(|e| format!("deno zip entry: {e}"))?;
        let name = f
            .enclosed_name()
            .and_then(|p| p.file_name().map(|n| n.to_string_lossy().to_string()))
            .unwrap_or_default();
        if name == wanted {
            // Same guard as download(): extract to .part and rename last, so
            // an interrupted unzip never leaves a truncated "installed" deno.
            let tmp = dest.with_extension("part");
            let mut out = std::fs::File::create(&tmp)
                .map_err(|e| format!("deno write: {e}"))?;
            std::io::copy(&mut f, &mut out)
                .map_err(|e| format!("deno extract: {e}"))?;
            drop(out);
            mark_executable(&tmp)?;
            std::fs::rename(&tmp, dest)
                .map_err(|e| format!("deno install: {e}"))?;
            return Ok(());
        }
    }
    Err("deno executable not found inside the release zip".to_string())
}

/// The directory holding our managed tools, so callers can put it on a
/// child process's PATH (that is how yt-dlp discovers deno).
pub fn managed_dir(app: &AppHandle) -> Option<PathBuf> {
    bin_dir(app).ok()
}

#[derive(serde::Serialize, Clone)]
pub struct Status {
    /// yt-dlp present (managed or on PATH). Required to run.
    pub ytdlp: bool,
    /// ffmpeg present. Required for full-quality merges.
    pub ffmpeg: bool,
    /// ffprobe present. Optional; only affects metadata richness.
    pub ffprobe: bool,
    /// Whether the worker has everything it needs to start.
    pub ready: bool,
}

/// Snapshot of which tools are available right now.
pub fn status(app: &AppHandle) -> Status {
    let ytdlp = resolve(app, "yt-dlp").is_some();
    let ffmpeg = resolve(app, "ffmpeg").is_some();
    let ffprobe = resolve(app, "ffprobe").is_some();
    Status {
        ytdlp,
        ffmpeg,
        ffprobe,
        ready: ytdlp && ffmpeg,
    }
}
