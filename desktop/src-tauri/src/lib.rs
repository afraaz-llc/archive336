// ARCHIVE336 — desktop worker.
//
// Runs in the user's tray. Once they log in we authenticate against the
// production API, then a background tokio task drains the sync-jobs
// queue: claim → run yt-dlp on the local machine (residential IP, cookies
// from the embedded Tauri webview's data store — see
// acquire_cookies_via_webview) → PUT the resulting .mp4 to R2 via the
// presigned URL the server hands us → POST /complete.
//
// This is the same protocol implemented in client/client.py — see that
// for the canonical reference. Differences here:
//   - Async (tokio) instead of threads + requests
//   - State held in tauri::State + emitted to the React UI as events
//   - yt-dlp is shelled out to via std::process::Command

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter, Manager, State, WebviewUrl, WebviewWindowBuilder};

mod binaries;
use tokio::sync::{watch, Mutex};

// ---------- Config + persistent state ----------

/// One connected YouTube account. Each gets its own isolated WKWebView
/// data store (cookie jar) keyed by `id`, so signing into account B never
/// clobbers account A's session. `channel_title` is filled in once we can
/// read it from that account (reported to the website in a later stage).
#[derive(Serialize, Deserialize, Clone, Debug)]
#[serde(rename_all = "camelCase")]
struct YoutubeAccount {
    id: String,
    #[serde(default)]
    channel_title: Option<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
#[serde(default)]
struct StoredConfig {
    base_url: String,
    username: String,
    password: String, // app data dir, mode 600 — local only
    /// Connected YouTube accounts. Empty on fresh installs and on configs
    /// written before multi-account existed (serde default), in which case
    /// the user connects accounts from the Connections tab.
    accounts: Vec<YoutubeAccount>,
    /// True only when the user deliberately switched launch-at-login OFF.
    /// The app turns autostart on by default (a backup tool that does not
    /// come back after a reboot is not a backup tool), and this flag is the
    /// one thing that stops that default from overriding somebody who said
    /// no. Nothing writes it but the Settings toggle.
    #[serde(rename = "autostartDeclined")]
    autostart_declined: bool,
    /// Channels this install has PROVEN it can see private videos on.
    ///
    /// Ownership used to be asserted by identity: ask YouTube "which
    /// channel am I signed in as" and report that one. A Google account
    /// owns many channels, and selecting a different one in YouTube's UI
    /// changes nothing the API tells us over cookies alone - it sets a
    /// delegated identity carried in a request header we never send. So
    /// a brand channel could never be authenticated, and the app's own
    /// advice ("click your avatar and choose Switch account") was
    /// impossible to act on.
    ///
    /// Proven by capability instead: only somebody with access sees a
    /// channel's private uploads, so seeing them IS the proof, and it is
    /// the exact capability the product needs rather than a proxy for it.
    #[serde(default, rename = "provenChannels")]
    proven_channels: Vec<String>,
    /// Channels the user has completed a sign-in for.
    ///
    /// Distinct from proven_channels, and deliberately weaker. Proven
    /// means we demonstrated this login can see the channel's private
    /// uploads, which is what the SERVER needs before it will unlock
    /// them - the video pool is shared, so a self-asserted claim there
    /// would let one subscriber read another's private titles.
    ///
    /// Linked means only "a YouTube login is attached to this channel",
    /// which is exactly what the Authenticate button asks for and all it
    /// can honestly promise. It drives the pill in this app. A channel
    /// with no videos yet, or none private today, is connected the
    /// moment the user signs in - which is the point: they may upload a
    /// private video tomorrow.
    #[serde(default, rename = "linkedChannels")]
    linked_channels: Vec<String>,
    /// channel id -> the delegated identity ("pageId") that reaches its
    /// private uploads.
    ///
    /// A Google account can act as several channels. Cookies alone always
    /// speak as the PRIMARY one, so a brand channel's private videos are
    /// invisible - AFRFX returned 498 of 498 through every cookie-based
    /// route we tried. YouTube selects the identity with an X-Goog-PageId
    /// header, which yt-dlp will carry via --add-header, and with the
    /// right one that same playlist returns 599.
    #[serde(default, rename = "channelPageIds")]
    channel_page_ids: std::collections::HashMap<String, String>,
}

impl Default for StoredConfig {
    fn default() -> Self {
        Self {
            base_url: "https://archive336.com".to_string(),
            username: String::new(),
            password: String::new(),
            accounts: Vec::new(),
            autostart_declined: false,
            proven_channels: Vec::new(),
            linked_channels: Vec::new(),
            channel_page_ids: Default::default(),
        }
    }
}

fn config_path(app: &AppHandle) -> Result<PathBuf, String> {
    let dir = app
        .path()
        .app_config_dir()
        .map_err(|e| format!("can't resolve config dir: {e}"))?;
    std::fs::create_dir_all(&dir).map_err(|e| format!("can't create config dir: {e}"))?;
    Ok(dir.join("config.json"))
}

fn load_config(app: &AppHandle) -> StoredConfig {
    let Ok(p) = config_path(app) else {
        return StoredConfig::default();
    };
    let Ok(s) = std::fs::read_to_string(&p) else {
        return StoredConfig::default();
    };
    let mut cfg: StoredConfig = serde_json::from_str(&s).unwrap_or_default();
    // base_url is no longer user-editable in the UI, but old configs
    // and dev overrides via the JSON file still drive what the worker
    // talks to. Empty / whitespace falls back to the production
    // default so a corrupted file can't lock the app out.
    if cfg.base_url.trim().is_empty() {
        cfg.base_url = StoredConfig::default().base_url;
    }
    cfg
}

fn save_config(app: &AppHandle, cfg: &StoredConfig) -> Result<(), String> {
    let p = config_path(app)?;
    let s = serde_json::to_string_pretty(cfg).map_err(|e| e.to_string())?;
    std::fs::write(&p, s).map_err(|e| format!("save config: {e}"))?;
    // Best-effort lock down to owner-only on unix
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o600));
    }
    Ok(())
}

/// Persist a brand-new account slot (fresh UUID) and return its id. The
/// slot starts disconnected; the caller opens its sign-in webview.
fn add_account_slot(app: &AppHandle) -> Result<String, String> {
    let mut cfg = load_config(app);
    let id = uuid::Uuid::new_v4().to_string();
    cfg.accounts.push(YoutubeAccount {
        id: id.clone(),
        channel_title: None,
    });
    save_config(app, &cfg)?;
    Ok(id)
}

/// Drop an account slot from persisted config (its cookie store is wiped
/// separately by the caller).
fn remove_account_slot(app: &AppHandle, account_id: &str) -> Result<(), String> {
    let mut cfg = load_config(app);
    cfg.accounts.retain(|a| a.id != account_id);
    save_config(app, &cfg)?;
    Ok(())
}

/// 16-byte WKWebView data-store identifier for an account, derived from
/// its UUID id so each account keeps its cookies in a separate jar.
/// (Isolation is macOS 14+ / iOS 17+; a no-op on Windows/Linux for now.)
fn account_store_id(id: &str) -> [u8; 16] {
    uuid::Uuid::parse_str(id)
        .map(|u| u.into_bytes())
        .unwrap_or([0u8; 16])
}

/// Persist an account's channel title (the active channel of its session).
/// Returns true when this actually changed the stored value.
fn set_account_channel_title(app: &AppHandle, account_id: &str, title: &str) -> bool {
    let mut cfg = load_config(app);
    let Some(slot) = cfg.accounts.iter_mut().find(|a| a.id == account_id) else {
        return false;
    };
    if slot.channel_title.as_deref() == Some(title) {
        return false;
    }
    slot.channel_title = Some(title.to_string());
    let _ = save_config(app, &cfg);
    true
}

// ---------- Runtime worker state, mirrored to UI ----------

#[derive(Serialize, Clone, Debug, Default)]
#[serde(rename_all = "camelCase")]
struct WorkerStatus {
    running: bool,
    logged_in: bool,
    current_job_id: Option<String>,
    current_video_id: Option<String>,
    current_video_title: Option<String>,
    current_progress: f32,
    last_completed_video_id: Option<String>,
    last_error: Option<String>,
    completed_count: u32,
}

struct AppData {
    status: Mutex<WorkerStatus>,
    cancel_tx: Mutex<Option<watch::Sender<bool>>>,
    http: reqwest::Client,
}

impl AppData {
    fn new() -> Self {
        // Identify the desktop app in the User-Agent so the Sessions
        // panel on the website can label it correctly. Include the
        // machine's hostname in parens so the row reads as e.g.
        // "Afraazs-MacBook-Pro" instead of a generic "Worker" line.
        let hostname = gethostname::gethostname()
            .into_string()
            .unwrap_or_else(|_| "unknown".to_string());
        let ua = format!(
            "ARCHIVE336-Archive-Tool-Desktop/{} ({})",
            env!("CARGO_PKG_VERSION"),
            hostname
        );
        let http = reqwest::Client::builder()
            .cookie_store(true)
            .timeout(Duration::from_secs(60))
            .user_agent(&ua)
            .build()
            .expect("build reqwest client");
        Self {
            status: Mutex::new(WorkerStatus::default()),
            cancel_tx: Mutex::new(None),
            http,
        }
    }
}

async fn emit_status(app: &AppHandle, state: &Arc<AppData>) {
    let s = state.status.lock().await.clone();
    let _ = app.emit("worker-status", s.clone());
    // Keep the menu-bar tray in sync with the same state the window sees.
    refresh_tray(app, &s);
}

// ---------- HTTP helpers ----------

#[derive(Deserialize, Debug)]
struct ClaimedJob {
    id: String,
    /// The channel this video belongs to. Sent by the server all along;
    /// the worker only started reading it when downloads needed to know
    /// which delegated identity to speak as.
    #[serde(rename = "channelId", default)]
    channel_id: String,
    #[serde(rename = "videoId")]
    video_id: String,
    /// 'video' for a normal mp4 + captions sync job, 'captions' for
    /// a captions-only backfill job, 'metadata' for a look-only refresh
    /// of the video's current record. Older server builds didn't send
    /// this; default to 'video' for backwards compat.
    #[serde(default = "default_job_kind")]
    kind: String,
    #[serde(rename = "youtubeUrl")]
    youtube_url: String,
    /// Presigned PUT URL for the mp4 upload. Server only mints one for
    /// video-kind jobs, so for every other kind this is empty.
    #[serde(rename = "uploadUrl", default)]
    upload_url: String,
    #[serde(rename = "uploadContentType")]
    upload_content_type: Option<String>,
    /// Presigned slot for the video's thumbnail. Private videos 404 on
    /// YouTube's CDN, so the worker is the only thing able to capture one.
    #[serde(rename = "thumbnailUploadUrl", default)]
    thumbnail_upload_url: String,
    /// Channel quality + captions settings, sent so the download honors
    /// what the user picked. Older servers don't send these; the defaults
    /// reproduce the previous hardcoded behavior.
    #[serde(rename = "maxResolution", default = "default_max_resolution")]
    max_resolution: String,
    #[serde(rename = "codecPreference", default = "default_codec_preference")]
    codec_preference: String,
    #[serde(rename = "saveCaptions", default = "default_save_captions")]
    save_captions: bool,
}

fn default_job_kind() -> String {
    "video".to_string()
}

fn default_max_resolution() -> String {
    "1080p".to_string()
}

fn default_codec_preference() -> String {
    "compat".to_string()
}

fn default_save_captions() -> bool {
    true
}

/// Translate the channel's maxResolution + codecPreference into a yt-dlp
/// format selector.
///
/// - compat  -> stay in the mp4/m4a (H.264/AAC) lane, maximum playability.
/// - efficient -> let yt-dlp pick the best stream regardless of container,
///   which is how AV1/VP9 get chosen when YouTube offers them.
/// - audio-only -> best audio; the caller remuxes to mp4 so the rest of the
///   pipeline (which expects video.mp4) still works.
fn ytdlp_format(max_resolution: &str, codec_preference: &str) -> String {
    if max_resolution == "audio-only" {
        return "ba[ext=m4a]/ba/best".to_string();
    }
    let height: Option<u32> = match max_resolution {
        "2160p" => Some(2160),
        "1440p" => Some(1440),
        "1080p" => Some(1080),
        "720p" => Some(720),
        "480p" => Some(480),
        "360p" => Some(360),
        // "source" (and anything unrecognized) => no cap, best available.
        _ => None,
    };
    let efficient = codec_preference == "efficient";
    match (height, efficient) {
        (Some(h), false) => format!(
            "bv*[ext=mp4][height<={h}]+ba[ext=m4a]/best[ext=mp4][height<={h}]/best"
        ),
        (None, false) => "bv*[ext=mp4]+ba[ext=m4a]/best[ext=mp4]/best".to_string(),
        (Some(h), true) => format!("bv*[height<={h}]+ba/best[height<={h}]/best"),
        (None, true) => "bv*+ba/best".to_string(),
    }
}

async fn login_request(
    http: &reqwest::Client,
    base: &str,
    username: &str,
    password: &str,
) -> Result<(), String> {
    let url = format!("{}/api/auth/login", base.trim_end_matches('/'));
    let res = http
        .post(&url)
        .json(&serde_json::json!({"username": username, "password": password}))
        .send()
        .await
        .map_err(|e| format!("login network error: {e}"))?;
    if !res.status().is_success() {
        let code = res.status();
        let body = res.text().await.unwrap_or_default();
        return Err(format!("login failed ({code}): {body}"));
    }
    Ok(())
}

/// Why a claim poll failed. Split out of the flat string it used to be
/// because a 401 is a completely different event from a 502: our session
/// expired (they last 30 days, and "sign out other devices" ends them
/// early) and we hold the credentials that can make a new one. Flattened
/// into a string, it read as just another transient error and the worker
/// polled a dead cookie until somebody thought to restart the app.
#[derive(Debug)]
enum ClaimError {
    /// 401/403 - the session is gone, not the network.
    Unauthorized,
    Other(String),
}

impl std::fmt::Display for ClaimError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ClaimError::Unauthorized => write!(f, "session expired"),
            ClaimError::Other(e) => write!(f, "{e}"),
        }
    }
}

async fn claim_job(http: &reqwest::Client, base: &str) -> Result<Option<ClaimedJob>, ClaimError> {
    let url = format!(
        "{}/api/youtube/sync-jobs/claim",
        base.trim_end_matches('/')
    );
    let res = http
        .get(&url)
        .send()
        .await
        .map_err(|e| ClaimError::Other(e.to_string()))?;
    let status = res.status();
    if status == reqwest::StatusCode::UNAUTHORIZED || status == reqwest::StatusCode::FORBIDDEN {
        return Err(ClaimError::Unauthorized);
    }
    if !status.is_success() {
        return Err(ClaimError::Other(format!("claim http {status}")));
    }
    let body = res.text().await.unwrap_or_default();
    if body.trim() == "null" || body.trim().is_empty() {
        return Ok(None);
    }
    let job: ClaimedJob = serde_json::from_str(&body)
        .map_err(|e| ClaimError::Other(format!("claim parse: {e}")))?;
    Ok(Some(job))
}

async fn heartbeat(
    http: &reqwest::Client,
    base: &str,
    job_id: &str,
    progress: f32,
) -> Result<(), String> {
    let url = format!(
        "{}/api/youtube/sync-jobs/{}/heartbeat",
        base.trim_end_matches('/'),
        job_id
    );
    let _ = http
        .post(&url)
        .json(&serde_json::json!({"progress": progress}))
        .send()
        .await
        .map_err(|e| format!("heartbeat: {e}"))?;
    Ok(())
}

/// Metadata about the downloaded mp4 the worker probes locally and
/// ships up with the complete-job call. Backend persists these onto
/// the UserChannelVideo row so the website's detail panel can show
/// resolution / codec / hash / aspect ratio / etc. All fields
/// optional - if ffprobe isn't on PATH or fails, we just don't
/// send them and the corresponding rows hide in the UI.
#[derive(Serialize, Default, Debug)]
struct FileMeta {
    #[serde(skip_serializing_if = "Option::is_none")]
    video_resolution: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    video_codec: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    video_bitrate_kbps: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    video_fps: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    audio_codec: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    audio_bitrate_kbps: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    container_format: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    sha256: Option<String>,
    /// Manual caption language codes (e.g. ["en", "es", "pt-BR"]).
    /// Empty list explicitly means "the video has no manual captions"
    /// - distinct from None which would mean "we didn't even try".
    /// Server unconditionally writes a captions block when this is
    /// present, so we always send it even if empty.
    #[serde(skip_serializing_if = "Option::is_none")]
    caption_languages: Option<Vec<String>>,
    /// The video's privacy as yt-dlp reports it (the info-json
    /// `availability`): public / unlisted / private / subscriber_only /
    /// premium_only / needs_auth. The backend maps this to our
    /// open/sealed visibility at archive time, so a public video lands
    /// Open and a private/members one Sealed - no dependence on the
    /// (broken) server-side channel scraper. None when yt-dlp didn't
    /// report it; the server then keeps its existing safe default.
    #[serde(skip_serializing_if = "Option::is_none")]
    availability: Option<String>,
    /// The real YouTube upload date (info-json `upload_date`, normalized to
    /// YYYY-MM-DD). Owner-private videos found via the uploads playlist
    /// carry no date from discovery, so the server otherwise stamps them
    /// with the moment they were found - making every one share a
    /// timestamp and any date sort meaningless.
    #[serde(skip_serializing_if = "Option::is_none")]
    upload_date: Option<String>,
    /// Bytes of the thumbnail we PUT to the presigned slot, so the server
    /// can record the key + bill it. Absent when we captured none.
    #[serde(skip_serializing_if = "Option::is_none")]
    thumbnail_bytes: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    description: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    tags: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    view_count: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    duration_sec: Option<u64>,
}

/// One comment as yt-dlp writes it into the info-json `comments` array
/// (verified against yt-dlp 2026.07.04). Field names are yt-dlp's own;
/// serde silently ignores the keys we don't map (author_thumbnail,
/// is_pinned, author_is_verified...). Everything but `id` and `parent` is
/// optional: an owner-private read can omit an author handle or a like
/// count, and defaulting one thin field is far better than failing the row.
#[derive(Deserialize, Debug)]
struct YtdlpComment {
    id: String,
    /// "root" for a top-level comment, else the parent comment's id. A reply
    /// id is itself "<parentId>.<childId>", identical to the YouTube Data API
    /// id the store already uses - so worker ids and OAuth-synced ids match.
    parent: String,
    #[serde(default)]
    author: Option<String>,
    /// yt-dlp's author_id IS the commenter's channel id (UCxxxx...), which is
    /// what the store keys is_by_uploader off.
    #[serde(default)]
    author_id: Option<String>,
    #[serde(default)]
    text: Option<String>,
    #[serde(default)]
    like_count: Option<i64>,
    /// Approximate unix seconds (yt-dlp derives it from "2 years ago"-style
    /// text, so it is not exact). Absent on some rows, which is why the
    /// reported publishedAt is nullable.
    #[serde(default)]
    timestamp: Option<i64>,
}

/// Everything one yt-dlp invocation produced. Grew past a tuple once we
/// started preserving the full record (description/tags/counts), which for
/// a URL-tracked channel has no other source.
struct YtdlpOutcome {
    mp4: Option<PathBuf>,
    captions: Vec<CaptionFile>,
    availability: Option<String>,
    upload_date: Option<String>,
    thumbnail: Option<PathBuf>,
    description: Option<String>,
    tags: Option<Vec<String>>,
    view_count: Option<u64>,
    duration_sec: Option<u64>,
    /// Title and poster URL as YouTube reports them right now. Only the
    /// metadata-kind path reads these - a video-kind completion carries
    /// the title through the archive record it writes.
    title: Option<String>,
    thumbnail_url: Option<String>,
    /// The full comment thread, present only when the run asked for it
    /// (--write-comments, i.e. a comments-kind job). Parsed all-or-nothing:
    /// see the info-json reader for why one bad row drops the whole vec.
    comments: Option<Vec<YtdlpComment>>,
    /// yt-dlp's top-level `comment_count` (YouTube's "N Comments" number).
    /// The backend sanity-checks the fetched item count against this.
    comment_count: Option<u64>,
    /// True when we had cookies but the run that actually succeeded was the
    /// signed-out retry. A download job is happy either way - the file is
    /// the file. A metadata job is NOT: a signed-out read is a different,
    /// thinner view of the same video, and the server versions whatever
    /// privacy string it is handed. yt-dlp answers `needs_auth` for an
    /// age-gated public video read anonymously, which the server maps to
    /// "private" - a fabricated privacy change on a video nobody touched.
    anonymous_fallback: bool,
}

/// The snapshot a 'metadata' job ships back: the video's record as YouTube
/// has it at this moment.
///
/// The first four fields are what the server VERSIONS - it diffs them
/// against what we archived and writes history from the difference, so an
/// absent one would read as a cleared value. Those are required, and a run
/// that misses any of them sends nothing at all (see metadata_snapshot()).
///
/// The rest are omitted from the JSON when yt-dlp did not report them,
/// which the server reads as "not looked at" and skips. Omitting is the
/// only honest encoding: an owner-private video routinely has no public
/// view count, and refusing to report ANYTHING for it would leave exactly
/// the videos this job exists for with no upkeep at all.
#[derive(Serialize, Debug)]
#[serde(rename_all = "camelCase")]
struct MetadataSnapshot {
    title: String,
    /// Empty string is a real value (a video with no description). It is
    /// only ever sent when yt-dlp actually reported the field.
    description: String,
    tags: Vec<String>,
    /// yt-dlp's `availability`: public / unlisted / private /
    /// subscriber_only / premium_only. Omitted when yt-dlp cannot confirm
    /// it - an age-restricted video reports availability "needs_auth" even
    /// though its title, description and tags all extract fine, and sinking
    /// a good read of everything else over an unconfirmable privacy label
    /// left every age-gated video with no upkeep. Privacy is optional the
    /// same way view_count is: the server leaves the stored value untouched
    /// when it is absent.
    #[serde(skip_serializing_if = "Option::is_none")]
    privacy: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    view_count: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    duration_sec: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    thumbnail_url: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    upload_date: Option<String>,
}

/// Completion body for a metadata job. Nested under "metadata" so the
/// server can tell a look-only refresh apart from a file report.
#[derive(Serialize, Debug)]
struct MetadataCompletion {
    metadata: MetadataSnapshot,
}

/// One comment as the backend store engine consumes it. camelCase on the
/// wire; the field NAMES match the engine keys in apply_comment_snapshot
/// (id, parentId, author, authorChannelId, text, likeCount, isEdited,
/// viewerRatingLike, publishedAt, updatedAt) exactly - the worker adapter
/// and the OAuth cron feed the identical shape. Every key is always sent
/// (no skip_serializing_if): the store reads each with a strict subscript,
/// so an omitted key would be a KeyError, and a null is a real value there.
#[derive(Serialize, Debug)]
#[serde(rename_all = "camelCase")]
struct CommentItem {
    id: String,
    /// null for a top-level comment (yt-dlp parent == "root"), else the
    /// parent comment's id.
    parent_id: Option<String>,
    author: String,
    author_channel_id: String,
    text: String,
    like_count: i64,
    /// yt-dlp exposes neither an edit flag nor a viewer rating, so both are
    /// defaulted false. is_edited is the OAuth cron's to flip (via its
    /// text-hash compare); the worker never asserts an edit.
    is_edited: bool,
    viewer_rating_like: bool,
    /// yt-dlp's unix `timestamp` rendered ISO-8601 UTC with a trailing Z,
    /// matching the YouTube Data API string the store already parses. null
    /// when yt-dlp reported no timestamp.
    published_at: Option<String>,
    /// yt-dlp has no per-comment updated_at; always null.
    updated_at: Option<String>,
}

/// The comment snapshot the worker certifies for one video. `complete`
/// gates the backend's soft-delete loop - only a cookie'd, uncapped, exit-0
/// fetch may diff-and-delete; anything less is insert/update-only.
/// `reported_total` is yt-dlp's comment_count, which the backend's sanity
/// ratio checks the item count against before trusting a deletion.
#[derive(Serialize, Debug)]
#[serde(rename_all = "camelCase")]
struct CommentsPayload {
    complete: bool,
    reported_total: u64,
    items: Vec<CommentItem>,
}

/// Completion body for a comments job. Nested under "comments" so the
/// server routes it to the comment store instead of a file report, exactly
/// as MetadataCompletion nests under "metadata".
#[derive(Serialize, Debug)]
struct CommentsCompletion {
    comments: CommentsPayload,
}

/// One manual caption track discovered by yt-dlp. `language` is the
/// BCP47-ish code yt-dlp put in the filename (en, en-US, pt-BR...),
/// `path` is the local .vtt file we'll PUT to R2.
#[derive(Debug)]
struct CaptionFile {
    language: String,
    path: PathBuf,
}

/// Run ffprobe -show_streams -show_format on the downloaded mp4 and
/// pull out the bits the user-facing panel cares about. Returns an
/// all-None FileMeta on any failure - we'd rather ship the video
/// without metadata than fail the whole job. ffprobe ships with ffmpeg
/// and ffmpeg is already a yt-dlp dep, so anyone who can sync videos
/// already has it.
async fn probe_mp4(app: &AppHandle, path: &std::path::Path) -> FileMeta {
    let mut out = FileMeta::default();
    // ffprobe is optional - if it isn't available we just return empty
    // metadata and the job still succeeds.
    let ffprobe = match binaries::resolve(app, "ffprobe") {
        Some(p) => p,
        None => return out,
    };
    let ff_out = match tokio::process::Command::new(&ffprobe)
        .arg("-v")
        .arg("quiet")
        .arg("-print_format")
        .arg("json")
        .arg("-show_streams")
        .arg("-show_format")
        .arg(path)
        .output()
        .await
    {
        Ok(o) if o.status.success() => o,
        Ok(o) => {
            log::warn!(
                "ffprobe non-zero exit: {}",
                String::from_utf8_lossy(&o.stderr)
            );
            return out;
        }
        Err(e) => {
            log::warn!("ffprobe spawn failed: {e}");
            return out;
        }
    };

    let json: serde_json::Value = match serde_json::from_slice(&ff_out.stdout) {
        Ok(v) => v,
        Err(e) => {
            log::warn!("ffprobe parse failed: {e}");
            return out;
        }
    };

    // Top-level format block: container + (sometimes) bitrate fallback.
    if let Some(fmt) = json.get("format") {
        if let Some(name) = fmt.get("format_name").and_then(|v| v.as_str()) {
            // ffprobe reports comma-joined for muxers; "mov,mp4,m4a,..."
            // The first token is the canonical one we want to show.
            out.container_format = Some(
                name.split(',').next().unwrap_or(name).to_string(),
            );
        }
    }

    // Streams: pick the first video stream and the first audio stream.
    if let Some(streams) = json.get("streams").and_then(|s| s.as_array()) {
        for stream in streams {
            let codec_type = stream.get("codec_type").and_then(|v| v.as_str());
            if codec_type == Some("video") && out.video_codec.is_none() {
                out.video_codec = stream
                    .get("codec_name")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string());
                let w = stream.get("width").and_then(|v| v.as_u64());
                let h = stream.get("height").and_then(|v| v.as_u64());
                if let (Some(w), Some(h)) = (w, h) {
                    out.video_resolution = Some(format!("{w}x{h}"));
                }
                if let Some(br) = stream.get("bit_rate").and_then(|v| v.as_str()) {
                    if let Ok(n) = br.parse::<u64>() {
                        out.video_bitrate_kbps = Some(n / 1000);
                    }
                }
                // FPS is a string like "30000/1001" - parse the ratio.
                if let Some(rate) =
                    stream.get("r_frame_rate").and_then(|v| v.as_str())
                {
                    if let Some((n, d)) = rate.split_once('/') {
                        if let (Ok(n), Ok(d)) = (n.parse::<f64>(), d.parse::<f64>()) {
                            if d > 0.0 {
                                // Round to one decimal to keep the
                                // display tidy (29.97 vs 30, etc).
                                out.video_fps = Some((n / d * 10.0).round() / 10.0);
                            }
                        }
                    }
                }
            } else if codec_type == Some("audio") && out.audio_codec.is_none() {
                out.audio_codec = stream
                    .get("codec_name")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string());
                if let Some(br) = stream.get("bit_rate").and_then(|v| v.as_str()) {
                    if let Ok(n) = br.parse::<u64>() {
                        out.audio_bitrate_kbps = Some(n / 1000);
                    }
                }
            }
        }
    }

    out
}

/// SHA-256 of the file. Streams the bytes through the hasher so big
/// videos don't hold the whole file in memory just for the hash.
async fn hash_file_sha256(path: &std::path::Path) -> Option<String> {
    use sha2::{Digest, Sha256};
    use tokio::io::AsyncReadExt;

    let mut file = match tokio::fs::File::open(path).await {
        Ok(f) => f,
        Err(e) => {
            log::warn!("hash_file open failed: {e}");
            return None;
        }
    };
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; 64 * 1024];
    loop {
        let n = match file.read(&mut buf).await {
            Ok(0) => break,
            Ok(n) => n,
            Err(e) => {
                log::warn!("hash_file read failed: {e}");
                return None;
            }
        };
        hasher.update(&buf[..n]);
    }
    Some(hex::encode(hasher.finalize()))
}

/// POST the completion body for a job. Generic over the body because the
/// kinds report different things through the same route: video/captions
/// jobs send a FileMeta, metadata jobs send a MetadataCompletion.
async fn complete_job<T: Serialize + ?Sized>(
    http: &reqwest::Client,
    base: &str,
    job_id: &str,
    body: &T,
) -> Result<(), String> {
    let url = format!(
        "{}/api/youtube/sync-jobs/{}/complete",
        base.trim_end_matches('/'),
        job_id
    );
    let res = http
        .post(&url)
        .json(body)
        .send()
        .await
        .map_err(|e| e.to_string())?;
    if !res.status().is_success() {
        return Err(format!(
            "complete http {}: {}",
            res.status(),
            res.text().await.unwrap_or_default()
        ));
    }
    Ok(())
}

async fn fail_job(
    http: &reqwest::Client,
    base: &str,
    job_id: &str,
    err: &str,
) -> Result<(), String> {
    let url = format!(
        "{}/api/youtube/sync-jobs/{}/fail",
        base.trim_end_matches('/'),
        job_id
    );
    let _ = http
        .post(&url)
        .json(&serde_json::json!({"error": err}))
        .send()
        .await;
    Ok(())
}

// ---------- yt-dlp + R2 upload ----------

/// Parse a yt-dlp subtitle filename like "video.en.vtt" or
/// "video.pt-BR.vtt" into its language code. Returns None for files
/// that don't fit the pattern (no embedded language).
fn parse_caption_language(filename: &str) -> Option<String> {
    let stem = filename.strip_suffix(".vtt")?;
    // yt-dlp's default subtitle name is `<output_base>.<lang>.vtt`, so
    // we look at the last dot-separated chunk before .vtt.
    let dot = stem.rfind('.')?;
    let lang = &stem[dot + 1..];
    if lang.is_empty() || lang == "video" {
        return None;
    }
    Some(lang.to_string())
}

/// Last `max` bytes of `s`, snapped forward to a char boundary.
///
/// yt-dlp's stderr quotes video titles, so it is full of emoji and
/// non-Latin scripts, and slicing it at a raw byte offset lands mid
/// character and panics - taking the whole worker task down with it while
/// the UI carried on saying "running". Never index a yt-dlp string
/// directly; come through here.
fn tail_bytes(s: &str, max: usize) -> &str {
    let mut cut = s.len().saturating_sub(max);
    while cut < s.len() && !s.is_char_boundary(cut) {
        cut += 1;
    }
    &s[cut..]
}

/// Run yt-dlp once and return (the mp4 path if downloaded, list of
/// manual caption files we wrote out). When `skip_download` is true
/// (captions-only backfill and metadata jobs) we pass --skip-download and
/// the returned mp4 path is None; the info-json sidecar is still written,
/// which is the whole point for a metadata job.
///
/// Captions: we always pass --write-subs --sub-langs all to grab every
/// MANUALLY-AUTHORED caption track. We deliberately do NOT pass
/// --write-auto-subs - YouTube's auto-generated speech-to-text
/// captions are noisy and the user only wants the real, human-edited
/// tracks. --convert-subs vtt normalizes the output to WebVTT in case
/// a video ships srt/ttml/srv3.
async fn run_ytdlp(
    app: &AppHandle,
    youtube_url: &str,
    out_dir: &std::path::Path,
    cookies_file: Option<&std::path::Path>,
    skip_download: bool,
    max_resolution: &str,
    codec_preference: &str,
    save_captions: bool,
    with_comments: bool,
    page_id: Option<&str>,
) -> Result<YtdlpOutcome, String> {
    let yt_dlp = binaries::resolve(app, "yt-dlp").ok_or_else(|| {
        "yt-dlp isn't set up yet - the app is still downloading its tools.".to_string()
    })?;

    let output_template = out_dir.join("video.%(ext)s");

    // Built per-attempt so we can retry without cookies. A stale webview
    // session makes YouTube answer "The page needs to be reloaded", which
    // fails even PUBLIC videos - passing bad cookies is worse than passing
    // none, so the retry below rescues public content.
    let build = |with_cookies: bool| {
        let mut cmd = tokio::process::Command::new(&yt_dlp);
        // Speak as the channel's own identity when we know it. Without
        // this a brand channel's private video 404s even with valid
        // cookies, exactly as its uploads playlist hides it.
        if let Some(pid) = page_id {
            cmd.arg("--add-header").arg(format!("X-Goog-PageId:{pid}"));
        }
        // Drop --no-warnings + --quiet so cookie-decrypt diagnostics get
        // captured into stderr. Without them yt-dlp swallows lines like
        // "Could not decrypt Chrome cookies" and the user sees only a
        // generic "Video unavailable" error.
        cmd.arg("-f")
            .arg(ytdlp_format(max_resolution, codec_preference))
            .arg("--merge-output-format")
            .arg("mp4")
            // Extractor args: tell yt-dlp to try alternative player
            // clients in order. Default handles public videos; web_creator
            // is YouTube Studio's client and can fetch the channel owner's
            // own private/unlisted videos when cookies are present;
            // mweb is a useful fallback for age-gated content.
            .arg("--extractor-args")
            .arg("youtube:player_client=default,web_creator,mweb")
            // Allow age-gated content. 99 is yt-dlp's effective 'no limit'.
            .arg("--age-limit")
            .arg("99");
        // Audio-only still has to land as video.mp4 - the rest of the
        // pipeline (upload key, probe, completion) expects that name.
        if max_resolution == "audio-only" {
            cmd.arg("--remux-video").arg("mp4");
        }
        // Caption flags: every manual caption track, normalized to WebVTT.
        // Auto-generated captions are excluded (no --write-auto-subs).
        // Skipped entirely when the channel's Captions toggle is off.
        if save_captions {
            cmd.arg("--write-subs")
                .arg("--sub-langs")
                .arg("all")
                .arg("--sub-format")
                .arg("vtt")
                .arg("--convert-subs")
                .arg("vtt");
        }
        cmd
            // Grab the poster frame too - the backend cannot fetch it for
            // private videos (YouTube's CDN 404s those).
            .arg("--write-thumbnail")
            .arg("--convert-thumbnails")
            .arg("jpg")
            // Write the metadata sidecar (video.info.json) so we can read
            // the real privacy (`availability`) and report it up.
            .arg("--write-info-json")
            .arg("-o")
            .arg(&output_template);
        // Point yt-dlp at our managed ffmpeg so it can merge the separate
        // video + audio streams. Passing the directory finds ffprobe too.
        if let Some(ffmpeg) = binaries::resolve(app, "ffmpeg") {
            if let Some(ffdir) = ffmpeg.parent() {
                cmd.arg("--ffmpeg-location").arg(ffdir);
            }
        }
        // yt-dlp discovers its JS runtime (deno) by searching PATH, and a
        // Finder-launched app inherits a bare PATH that contains none. Put
        // our managed tools dir first so the bundled deno is always found -
        // without it YouTube's n-challenge fails and we silently get ~360p
        // instead of the resolution the user asked for.
        if let Some(dir) = binaries::managed_dir(app) {
            let existing = std::env::var("PATH").unwrap_or_default();
            let sep = if cfg!(windows) { ";" } else { ":" };
            cmd.env("PATH", format!("{}{}{}", dir.display(), sep, existing));
        }
        if skip_download {
            cmd.arg("--skip-download");
        }
        // A comments job also pulls the full comment thread into the
        // info-json sidecar. Deliberately NO max_comments cap: the backend
        // may only diff-and-delete against a COMPLETE thread, so we fetch
        // the whole thing or the run does not certify completeness.
        if with_comments {
            cmd.arg("--write-comments");
        }
        if with_cookies {
            if let Some(path) = cookies_file {
                cmd.arg("--cookies").arg(path);
            }
        }
        cmd.arg(youtube_url);
        cmd
    };
    // Cookies come from the embedded-webview's data store via
    // acquire_cookies_via_webview() (written to a Netscape file).
    let mut out = build(cookies_file.is_some())
        .output()
        .await
        .map_err(|e| format!("spawn yt-dlp: {e}"))?;

    // Retry without cookies when a cookie'd attempt fails. A stale or
    // partially-valid webview session poisons the request badly enough that
    // YouTube refuses even public videos, so dropping the cookies is
    // strictly better than giving up. Private videos will still fail (they
    // genuinely need auth) - but public ones archive instead of stalling
    // until the user notices and re-signs in.
    let mut anonymous_fallback = false;
    if !out.status.success() && cookies_file.is_some() {
        let stderr = String::from_utf8_lossy(&out.stderr).to_string();
        let tail = tail_bytes(&stderr, 200);
        log::warn!("yt-dlp failed with cookies, retrying without: {tail}");
        if let Ok(retry) = build(false).output().await {
            if retry.status.success() {
                log::info!("yt-dlp succeeded without cookies (stale session?)");
                out = retry;
                anonymous_fallback = true;
            }
        }
    }

    if !out.status.success() {
        // Include the last 2KB of stderr so the server-side error
        // field captures cookie warnings, format errors, etc.
        let stderr = String::from_utf8_lossy(&out.stderr);
        let tail = tail_bytes(&stderr, 2048);
        return Err(format!("yt-dlp failed: {tail}"));
    }

    // Scan the output dir: find the mp4 (if any), any .vtt files, and the
    // info-json sidecar (for the real privacy).
    let mut mp4 = None;
    let mut captions: Vec<CaptionFile> = Vec::new();
    let mut availability: Option<String> = None;
    let mut upload_date: Option<String> = None;
    let mut thumbnail: Option<PathBuf> = None;
    let mut description: Option<String> = None;
    let mut tags: Option<Vec<String>> = None;
    let mut view_count: Option<u64> = None;
    let mut duration_sec: Option<u64> = None;
    let mut title: Option<String> = None;
    let mut thumbnail_url: Option<String> = None;
    let mut comments: Option<Vec<YtdlpComment>> = None;
    let mut comment_count: Option<u64> = None;
    for entry in std::fs::read_dir(out_dir).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let p = entry.path();
        let ext = p.extension().and_then(|s| s.to_str());
        match ext {
            Some("mp4") if mp4.is_none() => mp4 = Some(p),
            Some("jpg") | Some("jpeg") if thumbnail.is_none() => {
                thumbnail = Some(p)
            }
            Some("vtt") => {
                if let Some(name) = p.file_name().and_then(|s| s.to_str()) {
                    if let Some(lang) = parse_caption_language(name) {
                        captions.push(CaptionFile { language: lang, path: p });
                    }
                }
            }
            _ => {
                // --write-info-json sidecar (video.info.json): read the
                // `availability` field — yt-dlp's per-video privacy — so the
                // backend can resolve open vs sealed from the truth.
                if let Some(name) = p.file_name().and_then(|s| s.to_str()) {
                    if name.ends_with(".info.json") {
                        if let Ok(txt) = std::fs::read_to_string(&p) {
                            if let Ok(v) =
                                serde_json::from_str::<serde_json::Value>(&txt)
                            {
                                availability = v
                                    .get("availability")
                                    .and_then(|a| a.as_str())
                                    .map(|s| s.to_string());
                                // yt-dlp gives "YYYYMMDD"; normalize to
                                // YYYY-MM-DD so the server can store it
                                // directly as the real upload date.
                                upload_date = v
                                    .get("upload_date")
                                    .and_then(|a| a.as_str())
                                    .filter(|s| s.len() == 8 && s.chars().all(|c| c.is_ascii_digit()))
                                    .map(|s| format!("{}-{}-{}", &s[0..4], &s[4..6], &s[6..8]));
                                // The archive is meant to preserve the whole
                                // record, not just the file. For a channel
                                // tracked by URL there is no API path, so
                                // this sidecar is the ONLY source of the
                                // description / tags / counts.
                                description = v
                                    .get("description")
                                    .and_then(|a| a.as_str())
                                    .map(|s| s.to_string());
                                tags = v.get("tags").and_then(|a| a.as_array()).map(|arr| {
                                    arr.iter()
                                        .filter_map(|t| t.as_str().map(|s| s.to_string()))
                                        .collect::<Vec<String>>()
                                });
                                view_count = v.get("view_count").and_then(|a| a.as_u64());
                                duration_sec = v.get("duration").and_then(|a| a.as_f64()).map(|d| d as u64);
                                // Title + poster URL: what a metadata job
                                // exists to re-read. Absent means yt-dlp
                                // gave us a thin record, which the metadata
                                // path refuses to write rather than guess.
                                title = v
                                    .get("title")
                                    .and_then(|a| a.as_str())
                                    .map(|s| s.to_string());
                                thumbnail_url = v
                                    .get("thumbnail")
                                    .and_then(|a| a.as_str())
                                    .map(|s| s.to_string());
                                // Present only on a comments-kind run
                                // (--write-comments). comment_count is
                                // YouTube's top-level count; the backend
                                // sanity-ratios the fetched size against it.
                                comment_count =
                                    v.get("comment_count").and_then(|a| a.as_u64());
                                // Parse the thread ALL-or-NOTHING. If a single
                                // row is malformed the whole vec fails to None,
                                // which the backend reads as an empty fetch and
                                // its empty-snapshot short-circuit refuses to
                                // delete against. Dropping one row instead would
                                // make that row look deleted on a `complete`
                                // pass - a false "your comment was removed",
                                // the one output this feature must never emit.
                                comments = v
                                    .get("comments")
                                    .cloned()
                                    .and_then(|c| {
                                        serde_json::from_value::<Vec<YtdlpComment>>(c).ok()
                                    });
                            }
                        }
                    }
                }
            }
        }
    }

    if !skip_download && mp4.is_none() {
        return Err("yt-dlp produced no .mp4".to_string());
    }
    Ok(YtdlpOutcome {
        mp4,
        captions,
        availability,
        upload_date,
        thumbnail,
        description,
        tags,
        view_count,
        duration_sec,
        title,
        thumbnail_url,
        comments,
        comment_count,
        anonymous_fallback,
    })
}

/// Turn a yt-dlp run into the snapshot a metadata job reports, or list the
/// versioned fields it did not give us.
///
/// All-or-nothing across the VERSIONED fields on purpose. The server reads
/// an absent one as a cleared value and writes history saying the user
/// deleted it, so a thin extraction must produce NOTHING rather than a
/// half-filled record. The distinction that matters: a description of "" or
/// an empty tag list are real values yt-dlp reported, while a missing KEY
/// means we could not look.
///
/// The unversioned extras (view count, duration, poster url, upload date)
/// are carried only when reported and left out of the payload otherwise.
/// They cannot produce a false edit: the server skips a field it was not
/// sent, and only ever fills a hole with duration / upload date rather than
/// overwriting. Requiring them would fail the whole job over a stat YouTube
/// does not publish for private videos - which is most of what this path
/// exists to look after.
fn metadata_snapshot(o: YtdlpOutcome) -> Result<MetadataSnapshot, Vec<&'static str>> {
    let mut missing: Vec<&'static str> = Vec::new();
    // Blank is never legitimate here: there is no such thing as a YouTube
    // video with no title, so an empty one means the extraction came back
    // thin rather than that the field is really empty.
    let title = o.title.filter(|s| !s.trim().is_empty());
    if title.is_none() {
        missing.push("title");
    }
    if o.description.is_none() {
        missing.push("description");
    }
    if o.tags.is_none() {
        missing.push("tags");
    }
    // Privacy is deliberately NOT in the required set. yt-dlp answers
    // "needs_auth" for an age-restricted video whose title, description and
    // tags all extract fine, and only a confirmed reading is worth acting
    // on. An unconfirmable one is dropped here so the server leaves the
    // stored privacy alone, exactly as it does for an absent view count.
    let privacy = o
        .availability
        .filter(|a| a != "needs_auth" && !a.trim().is_empty());
    // Matched rather than unwrapped so the struct can only be built from
    // values that are actually present - the checks above and the fields
    // below can't drift apart into a silent default.
    match (title, o.description, o.tags) {
        (Some(title), Some(description), Some(tags)) => {
            Ok(MetadataSnapshot {
                title,
                description,
                tags,
                privacy,
                view_count: o.view_count,
                duration_sec: o.duration_sec,
                thumbnail_url: o.thumbnail_url.filter(|s| !s.trim().is_empty()),
                upload_date: o.upload_date,
            })
        }
        _ => Err(missing),
    }
}

async fn upload_to_r2(
    http: &reqwest::Client,
    upload_url: &str,
    content_type: &str,
    file_path: &std::path::Path,
) -> Result<(), String> {
    let bytes = tokio::fs::read(file_path)
        .await
        .map_err(|e| format!("read file: {e}"))?;
    // Override the client's default 60s timeout for this PUT - video
    // files run hundreds of MB and slow residential uplinks need a
    // proper window to push them through. 30 minutes is the cap; if
    // it really takes longer, something's wrong.
    let res = http
        .put(upload_url)
        .header("Content-Type", content_type)
        .timeout(Duration::from_secs(1800))
        .body(bytes)
        .send()
        .await
        .map_err(|e| format!("r2 put: {e}"))?;
    if !res.status().is_success() {
        return Err(format!(
            "r2 put http {}: {}",
            res.status(),
            res.text().await.unwrap_or_default()
        ));
    }
    Ok(())
}

#[derive(Deserialize, Debug)]
struct CaptionUploadResp {
    #[serde(rename = "uploadUrl")]
    upload_url: String,
    #[serde(rename = "uploadContentType")]
    upload_content_type: String,
}

/// Ask the server for a presigned PUT URL for a single VTT file, then
/// upload the file. The server enforces job ownership + caps the
/// language code, so we don't have to. Returns Err on any step that
/// fails - the caller decides whether to abort the whole job or just
/// log + continue.
async fn upload_caption(
    http: &reqwest::Client,
    base: &str,
    job_id: &str,
    caption: &CaptionFile,
) -> Result<(), String> {
    let url = format!(
        "{}/api/youtube/sync-jobs/{}/caption-upload-url",
        base.trim_end_matches('/'),
        job_id
    );
    // Hash the .vtt so the server can keep caption history only when the
    // transcript actually changed (older servers ignore the extra field).
    let sha = hash_file_sha256(&caption.path).await;
    let res = http
        .post(&url)
        .json(&serde_json::json!({"language": caption.language, "sha256": sha}))
        .send()
        .await
        .map_err(|e| format!("caption-upload-url: {e}"))?;
    if !res.status().is_success() {
        return Err(format!(
            "caption-upload-url http {}: {}",
            res.status(),
            res.text().await.unwrap_or_default()
        ));
    }
    let resp: CaptionUploadResp = res
        .json()
        .await
        .map_err(|e| format!("caption-upload-url parse: {e}"))?;
    upload_to_r2(
        http,
        &resp.upload_url,
        &resp.upload_content_type,
        &caption.path,
    )
    .await
}

// ---------- The worker loop ----------

/// List a playlist's video ids + titles via yt-dlp --flat-playlist (fast,
/// no per-video extraction). With owner cookies on the uploads playlist
/// (UU…) this includes private/unlisted videos that the public tab hides.
/// How long a single playlist enumeration may run before we give up.
///
/// Measured: 7,264 entries in 27s from a real uploads playlist, so ten
/// minutes is enormous headroom even for a 20,000-video channel. The
/// timeout is not about slowness, it is about a hang: this call sits in
/// front of claim_job in the worker loop, so one wedged yt-dlp would park
/// the worker and it would never claim another job for the life of the
/// process.
const PLAYLIST_ENUMERATION_TIMEOUT: Duration = Duration::from_secs(600);

/// Enumerate a playlist with yt-dlp, newest first.
///
/// Cookies are OPTIONAL and that is the whole point. A public uploads
/// playlist needs no credentials at all - this used to take a required
/// &Path and pass --cookies unconditionally, so a user who had not signed
/// in to Google got no catalogue enumerated whatsoever, not even the
/// public videos that need nothing. With owner cookies the same call
/// additionally surfaces private and unlisted uploads.
///
/// `approximate_date` synthesizes upload_date from YouTube's relative
/// dates. Without it every row comes back dateless and the server's
/// chronological queueing degenerates to insertion order.
async fn list_playlist_videos(
    app: &AppHandle,
    playlist_id: &str,
    cookies_file: Option<&std::path::Path>,
    // The delegated identity to speak as. Cookies alone always speak as
    // the Google account's PRIMARY channel, so without this a brand
    // channel's private uploads are simply absent from the response.
    page_id: Option<&str>,
) -> Vec<serde_json::Value> {
    let Some(yt_dlp) = binaries::resolve(app, "yt-dlp") else {
        return Vec::new();
    };
    let url = format!("https://www.youtube.com/playlist?list={playlist_id}");
    let mut cmd = tokio::process::Command::new(yt_dlp);
    if let Some(pid) = page_id {
        cmd.arg("--add-header").arg(format!("X-Goog-PageId:{pid}"));
    }
    cmd.arg("--flat-playlist")
        .arg("--ignore-errors")
        .arg("--no-warnings")
        .arg("--extractor-args")
        .arg("youtubetab:approximate_date")
        .arg("--print")
        .arg("%(id)s\t%(upload_date)s\t%(title)s");
    if let Some(cf) = cookies_file {
        cmd.arg("--cookies").arg(cf);
    }
    cmd.arg(&url);

    let out = match tokio::time::timeout(
        PLAYLIST_ENUMERATION_TIMEOUT,
        cmd.output(),
    )
    .await
    {
        Ok(Ok(o)) => o,
        Ok(Err(e)) => {
            log::warn!("playlist list failed: {e}");
            return Vec::new();
        }
        Err(_) => {
            log::warn!(
                "playlist {playlist_id} enumeration timed out; skipping"
            );
            return Vec::new();
        }
    };
    String::from_utf8_lossy(&out.stdout)
        .lines()
        .filter_map(|line| {
            let mut parts = line.splitn(3, '\t');
            let id = parts.next()?.trim();
            if id.is_empty() {
                return None;
            }
            // "20260727" -> "2026-07-27". yt-dlp prints NA when it cannot
            // work one out; send nothing rather than a fake date.
            let raw_date = parts.next().unwrap_or("").trim();
            let upload_date = if raw_date.len() == 8
                && raw_date.chars().all(|c| c.is_ascii_digit())
            {
                format!(
                    "{}-{}-{}",
                    &raw_date[0..4],
                    &raw_date[4..6],
                    &raw_date[6..8]
                )
            } else {
                String::new()
            };
            let title = parts.next().unwrap_or("").trim();
            Some(serde_json::json!({
                "id": id,
                "title": title,
                "uploadDate": upload_date,
            }))
        })
        .collect()
}

/// For each channel the user owns (per the backend), enumerate its uploads
/// playlist with the worker's cookies — surfacing private/unlisted videos —
/// and report them to the backend, which queues the new ones to sync.
/// Best-effort; uses the first connected account's cookies (per-account
/// routing for channels owned by a non-primary login is a follow-up).
/// Export cookies for any connected account, or None if there are none.
///
/// Returns None rather than waiting: a worker with no YouTube account can
/// still back up every public video, which is most of what most channels
/// are. Clears a stale export on the way out so a signed-out account's
/// file is never handed to yt-dlp.
async fn acquire_cookies_if_any(
    app: &AppHandle,
    cookies_path: &std::path::Path,
) -> Option<PathBuf> {
    let accounts = load_config(app).accounts;
    match export_any_account_cookies(app, &accounts, cookies_path).await {
        Some(n) => {
            log::info!("acquired {n} cookies via embedded webview");
            Some(cookies_path.to_path_buf())
        }
        None => {
            let _ = std::fs::remove_file(cookies_path);
            None
        }
    }
}
/// Enumerate every channel the user TRACKS and report what is there.
///
/// Two things changed here and both matter.
///
/// It used to read /worker/owned-channels, so only channels the user had
/// authenticated were ever enumerated. And it was called only when cookies
/// existed. Together that meant a user who tracked a channel and never
/// signed in to Google had NOTHING discovered - not even the public
/// videos, which need no credentials at all. Their archive stayed empty
/// and nothing on any screen explained why.
///
/// Now it walks the tracked list, and cookies are an upgrade rather than
/// an entry requirement: without them we enumerate the public catalogue,
/// with them the same call also surfaces private and unlisted uploads.
async fn discover_tracked_channels(
    app: &AppHandle,
    state: &Arc<AppData>,
    cfg: &StoredConfig,
    cookies_file: Option<&std::path::Path>,
) {
    let base = cfg.base_url.trim_end_matches('/');
    let url = format!("{base}/api/youtube/worker/tracked-channels");
    let body: serde_json::Value = match state.http.get(&url).send().await {
        Ok(r) if r.status().is_success() => match r.json::<serde_json::Value>().await
        {
            Ok(v) => v,
            Err(_) => return,
        },
        _ => return,
    };
    let channels = body
        .get("channels")
        .and_then(|c| c.as_array())
        .cloned()
        .unwrap_or_default();

    for entry in channels {
        let Some(ch) = entry.get("youtubeId").and_then(|v| v.as_str()) else {
            continue;
        };
        if !ch.starts_with("UC") || ch.len() < 3 {
            continue;
        }
        // Revoked means the user withdrew our access on the website. Their
        // cookies are already gone, but enumerate the public set anyway -
        // withdrawing access to private videos is not a request to stop
        // backing up the public ones.
        let revoked = entry
            .get("revoked")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let authenticated = entry
            .get("authenticated")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let use_cookies = if revoked || !authenticated {
            None
        } else {
            cookies_file
        };

        let uploads = format!("UU{}", &ch[2..]);
        let page_id = load_config(app).channel_page_ids.get(ch).cloned();
        let videos =
            list_playlist_videos(app, &uploads, use_cookies, page_id.as_deref()).await;
        if videos.is_empty() {
            continue;
        }
        log::info!(
            "enumerated {} videos for {ch} ({})",
            videos.len(),
            if use_cookies.is_some() { "with cookies" } else { "public only" }
        );

        let report_url = format!("{base}/api/youtube/worker/discovered-videos");
        let payload = serde_json::json!({
            "channelId": ch,
            "videos": videos,
            // Tells the server these were enumerated without credentials,
            // so it records them at their real public visibility instead of
            // stamping the whole catalogue private.
            "authenticated": use_cookies.is_some(),
        });
        match state.http.post(&report_url).json(&payload).send().await {
            Ok(r) if r.status().is_success() => {
                if let Ok(v) = r.json::<serde_json::Value>().await {
                    log::info!(
                        "reported {ch}: {} newly discovered",
                        v.get("discovered").and_then(|d| d.as_u64()).unwrap_or(0)
                    );
                }
            }
            Ok(r) => log::warn!("discovered-videos HTTP {}", r.status()),
            Err(e) => log::warn!("discovered-videos failed: {e}"),
        }
    }
}

/// Write the first connected account's cookies to `path` for yt-dlp's
/// --cookies flag, trying each account until one yields a usable session.
/// Returns the cookie count, or None when no account could supply one.
///
/// Routing a job to the specific account that owns its video (for private
/// videos spread across multiple accounts) is a later refinement; any
/// connected account's cookies cover that account's own public +
/// owner-private videos.
async fn export_any_account_cookies(
    app: &AppHandle,
    accounts: &[YoutubeAccount],
    path: &std::path::Path,
) -> Option<usize> {
    for acct in accounts {
        match acquire_cookies_via_webview(app, &acct.id, path).await {
            Ok(n) => return Some(n),
            Err(e) => log::warn!("account {} cookies unavailable: {e}", acct.id),
        }
    }
    None
}

/// How often a running worker re-checks the website for revoked channels.
const REVOCATION_CHECK_INTERVAL: Duration = Duration::from_secs(300);

/// How often a running worker re-reads the tracked-channel list and
/// enumerates it.
///
/// Five minutes, matching the revocation check: both exist because the
/// website is where the user changes things and the worker cannot be
/// told. Enumeration is a flat-playlist call per channel - measured at
/// 7,264 entries in 27s - so this is cheap next to the downloads it
/// feeds, and the cost of being wrong is a channel that silently never
/// syncs.
const DISCOVERY_INTERVAL: Duration = Duration::from_secs(300);

/// How often a claimed job re-asserts that it is still alive.
///
/// Well under the server's HEARTBEAT_STALE_SECONDS (300). We used to send
/// exactly ONE heartbeat per job, at the halfway mark - after the download
/// finished and before probe/hash/upload. So any video whose download ran
/// longer than five minutes was reapable while it was actively
/// downloading: the server handed the job to another claim, the original
/// worker finished, PUT the file, and got a 409. The bigger the video, the
/// more certain it could never be archived.
const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(60);

/// Keeps a job claimed for as long as the guard is alive.
///
/// Drop aborts the task, so a keepalive can never outlive its job and hold
/// a claim on work the worker already finished or abandoned. process_job
/// has several early returns; a Drop guard is the only shape that covers
/// all of them without threading cleanup through each one.
struct HeartbeatGuard(tokio::task::JoinHandle<()>);

impl Drop for HeartbeatGuard {
    fn drop(&mut self) {
        self.0.abort();
    }
}

fn spawn_heartbeat(
    state: &Arc<AppData>,
    base: String,
    job_id: String,
) -> HeartbeatGuard {
    let state = Arc::clone(state);
    HeartbeatGuard(tokio::spawn(async move {
        let mut tick = tokio::time::interval(HEARTBEAT_INTERVAL);
        tick.tick().await; // fires immediately; /claim just stamped one
        loop {
            tick.tick().await;
            let progress = { state.status.lock().await.current_progress };
            if let Err(e) = heartbeat(&state.http, &base, &job_id, progress).await
            {
                log::warn!("keepalive heartbeat failed for {job_id}: {e}");
            }
        }
    }))
}

/// Backoff for the startup handshake. Launch-at-login puts the worker on
/// the network three seconds into the boot, usually before wifi has
/// associated, so every step of the handshake waits and tries again
/// instead of returning. Starts polite, caps at five minutes so a machine
/// that is offline all afternoon costs one request per five minutes.
const STARTUP_BACKOFF_START: Duration = Duration::from_secs(10);
const STARTUP_BACKOFF_MAX: Duration = Duration::from_secs(300);

/// How many times in a row we re-login on a 401 before deciding the
/// credentials themselves are wrong. Bounded so a changed password
/// surfaces as an error the user can act on rather than an endless
/// login loop against the server.
const MAX_REAUTH_ATTEMPTS: u32 = 5;

/// Sleep out a backoff interval, waking early if the worker is cancelled.
/// Returns false when cancelled, which every caller reads as "stop now".
/// Doubles `delay` on the way out, up to STARTUP_BACKOFF_MAX.
async fn backoff_sleep(delay: &mut Duration, cancel: &mut watch::Receiver<bool>) -> bool {
    if *cancel.borrow() {
        return false;
    }
    tokio::select! {
        _ = tokio::time::sleep(*delay) => {}
        _ = cancel.changed() => return false,
    }
    *delay = (*delay * 2).min(STARTUP_BACKOFF_MAX);
    true
}

/// The worker stopped for real. Clears every field that describes work in
/// progress, because a stopped worker still showing a job id is the tray
/// telling the user "Syncing..." about nothing.
async fn mark_worker_stopped(app: &AppHandle, state: &Arc<AppData>) {
    let mut s = state.status.lock().await;
    s.running = false;
    s.current_job_id = None;
    s.current_video_id = None;
    s.current_progress = 0.0;
    drop(s);
    emit_status(app, state).await;
}

async fn worker_loop(
    app: AppHandle,
    state: Arc<AppData>,
    mut cfg: StoredConfig,
    mut cancel: watch::Receiver<bool>,
) {
    log::info!("worker starting against {}", cfg.base_url);

    // Login first, retrying until it works. This used to return on the
    // first failure, which meant a worker started at login - before the
    // network was up - was dead until the user opened the app and pressed
    // Start. `running` deliberately stays true: retrying IS running, and
    // last_error carries what we are stuck on, so the UI says something
    // true either way.
    let mut delay = STARTUP_BACKOFF_START;
    loop {
        match login_request(&state.http, &cfg.base_url, &cfg.username, &cfg.password).await {
            Ok(()) => break,
            Err(e) => {
                log::warn!("login failed, retrying in {}s: {e}", delay.as_secs());
                {
                    let mut s = state.status.lock().await;
                    s.logged_in = false;
                    s.last_error = Some(format!("login failed: {e}"));
                }
                emit_status(&app, &state).await;
                if !backoff_sleep(&mut delay, &mut cancel).await {
                    mark_worker_stopped(&app, &state).await;
                    return;
                }
                // Re-read the credentials before trying again. Retrying keeps
                // `running` true, and do_start_worker refuses to replace a
                // running worker, so a password the user has just corrected in
                // Settings has no other way in: without this, a changed
                // password wedges the app against the old one until it is
                // quit and relaunched. Only adopt a config that still holds a
                // real login - an unreadable file reads back as the empty
                // default, and taking that would throw away the credentials
                // we were started with.
                let fresh = load_config(&app);
                if !fresh.username.is_empty()
                    && !fresh.password.is_empty()
                    && (fresh.username != cfg.username || fresh.password != cfg.password)
                {
                    log::info!("credentials changed while retrying; using the new ones");
                    cfg = fresh;
                    delay = STARTUP_BACKOFF_START;
                }
            }
        }
    }
    {
        let mut s = state.status.lock().await;
        s.logged_in = true;
        s.last_error = None;
    }
    emit_status(&app, &state).await;

    // Cookie probe no longer needed — the embedded webview is its own
    // session, doesn't require keychain access to the user's daily
    // browser, and acquire_cookies_via_webview() does its own
    // validation against the same data store.

    // Auto-retry any sync_jobs left in 'failed' state. The server
    // re-enqueues them as fresh pending jobs and resets the visible
    // video status from 'failed' back to 'discovered', so the user
    // doesn't have to click Retry on each card after fixing whatever
    // caused the original failures (cookies, yt-dlp flags, etc).
    let retry_url = format!(
        "{}/api/youtube/sync-jobs/retry-failed",
        cfg.base_url.trim_end_matches('/')
    );
    match state.http.post(&retry_url).send().await {
        Ok(res) if res.status().is_success() => {
            if let Ok(body) = res.json::<serde_json::Value>().await {
                let count = body.get("retried").and_then(|v| v.as_u64()).unwrap_or(0);
                if count > 0 {
                    log::info!("re-enqueued {count} previously-failed sync jobs");
                }
            }
        }
        Ok(res) => log::warn!("retry-failed returned {}", res.status()),
        Err(e) => log::warn!("retry-failed request error: {e}"),
    }

    // Reconcile with the website BEFORE any cookie touches disk: if the user
    // revoked a channel, that account gets signed out here first, so the
    // export below can't pick up a session we were told to let go of.
    report_and_apply_revocations(&app, &state).await;

    // Where the Netscape cookie file for yt-dlp's --cookies flag lives.
    // Resolving the data dir is not something that fails twice for
    // different reasons, but it is also not worth ending a background
    // worker over, so it waits like everything else here.
    let mut delay = STARTUP_BACKOFF_START;
    let data_dir: PathBuf = loop {
        match app.path().app_local_data_dir() {
            Ok(dir) => break dir,
            Err(e) => {
                log::error!("no app_local_data_dir: {e}");
                {
                    let mut s = state.status.lock().await;
                    s.last_error = Some(format!("can't resolve app data dir: {e}"));
                }
                emit_status(&app, &state).await;
                if !backoff_sleep(&mut delay, &mut cancel).await {
                    mark_worker_stopped(&app, &state).await;
                    return;
                }
            }
        }
    };

    // Pull cookies out of the embedded webview's data store, if there are
    // any. Crucially this does NOT block: cookies unlock a channel's
    // PRIVATE videos, they are not a precondition for syncing at all.
    //
    // This used to spin here forever with "No connected YouTube account",
    // so a user who tracked a channel on the website and never signed in
    // to Google got nothing backed up - not even the public videos, which
    // need no credentials whatsoever. yt-dlp takes cookies as an option
    // and already retries without them, the server queues the open set for
    // an unauthenticated user, and the whole access model is built around
    // open-vs-sealed. The only thing insisting on cookies was this loop.
    //
    // Re-acquired periodically below, so authenticating later starts the
    // private videos flowing without a restart.
    let cookies_path = data_dir.join("yt-cookies.txt");
    let mut cookies_file_path: Option<PathBuf> =
        acquire_cookies_if_any(&app, &cookies_path).await;
    if cookies_file_path.is_none() {
        log::info!(
            "no connected YouTube account; syncing public videos only"
        );
    }

    // Waiting above cleared, so drop the sticky error and re-read the
    // config: the account list is whatever survived the reconcile plus
    // anything the user connected while we waited.
    {
        let mut s = state.status.lock().await;
        s.last_error = None;
    }
    emit_status(&app, &state).await;
    let cfg = load_config(&app);

    // Enumerate every tracked channel and report what is there, so the
    // server can queue the back catalogue. Runs whether or not we have
    // cookies - without them we still see the whole public catalogue,
    // which is most of what most channels are. Best-effort.
    discover_tracked_channels(&app, &state, &cfg, cookies_file_path.as_deref())
        .await;

    // Revocation is a decision the user makes on the website, and they
    // expect the app to let go of the channel then - not at the next
    // launch. Re-check on a slow interval; one account_menu call per
    // connected account, so it stays cheap next to the job work.
    let mut next_revocation_check = std::time::Instant::now() + REVOCATION_CHECK_INTERVAL;

    // Re-enumerate on a slow interval.
    //
    // Discovery used to run exactly twice: once at startup, and once if a
    // YouTube account connected later. So a channel added or authorized
    // while the app was open was never scanned at all - the owner
    // authorized his last channel, unpaused it, watched nothing happen,
    // and the only cure was quitting the app. The website is the source
    // of the channel list and the user edits it there, so the worker has
    // to keep asking rather than assume it heard everything at launch.
    let mut next_discovery = std::time::Instant::now() + DISCOVERY_INTERVAL;

    // Consecutive failed re-logins after a 401. Reset by any successful
    // poll, so only a real run of failures counts toward the bound.
    let mut reauth_attempts: u32 = 0;

    loop {
        if *cancel.borrow() {
            break;
        }
        if std::time::Instant::now() >= next_discovery {
            next_discovery = std::time::Instant::now() + DISCOVERY_INTERVAL;
            let cfg_now = load_config(&app);
            discover_tracked_channels(
                &app,
                &state,
                &cfg_now,
                cookies_file_path.as_deref(),
            )
            .await;
        }
        if std::time::Instant::now() >= next_revocation_check {
            next_revocation_check = std::time::Instant::now() + REVOCATION_CHECK_INTERVAL;
            let before = load_config(&app).accounts.len();
            report_and_apply_revocations(&app, &state).await;
            let remaining = load_config(&app).accounts;
            if remaining.len() != before {
                // An account was just signed out, but the exported cookies
                // file still holds its session. Replace it with one from an
                // account we're still allowed to use, or stop - carrying on
                // would mean running on revoked credentials.
                if let Some(path) = cookies_file_path.as_deref() {
                    match export_any_account_cookies(&app, &remaining, path).await {
                        Some(n) => {
                            log::info!("re-exported {n} cookies after a revocation")
                        }
                        None => {
                            // Revoked down to zero accounts. Drop the
                            // cookies and keep going on the public set
                            // rather than stopping: revoking access to
                            // private videos is not a request to stop
                            // backing up the public ones, and stopping
                            // here used to take the whole worker down
                            // with it.
                            let _ = std::fs::remove_file(path);
                            log::info!(
                                "no usable YouTube account left; \
                                 continuing with public videos only"
                            );
                            cookies_file_path = None;
                        }
                    }
                }
            }
            // Authenticated since we last looked? Take the cookies and
            // enumerate the private uploads straight away, so connecting
            // a channel starts its sealed videos without a restart.
            if cookies_file_path.is_none() {
                cookies_file_path =
                    acquire_cookies_if_any(&app, &cookies_path).await;
                if let Some(cf) = cookies_file_path.as_deref() {
                    log::info!("YouTube account connected; private videos now included");
                    discover_tracked_channels(&app, &state, &cfg, Some(cf)).await;
                }
            }
        }
        let claim = claim_job(&state.http, &cfg.base_url).await;
        match claim {
            Err(ClaimError::Unauthorized) => {
                // Our session died under us - it hit the 30-day cap, or the
                // user pressed "sign out other devices". We still hold the
                // credentials that made it, so log back in and claim again
                // next tick instead of polling a dead cookie forever.
                reauth_attempts += 1;
                if reauth_attempts > MAX_REAUTH_ATTEMPTS {
                    // Five refusals in a row is not an expired session, it is
                    // credentials that no longer work (changed password,
                    // deleted account). Say so and stop rather than hammer
                    // the login endpoint behind a UI that claims all is well.
                    log::error!("re-login refused {reauth_attempts} times; stopping worker");
                    let mut s = state.status.lock().await;
                    s.logged_in = false;
                    s.last_error = Some(
                        "Signed out. Your password may have changed - sign in again."
                            .to_string(),
                    );
                    drop(s);
                    emit_status(&app, &state).await;
                    break;
                }
                log::warn!("claim unauthorized; re-logging in (attempt {reauth_attempts})");
                match login_request(&state.http, &cfg.base_url, &cfg.username, &cfg.password)
                    .await
                {
                    Ok(()) => {
                        log::info!("re-login succeeded, resuming");
                        let mut s = state.status.lock().await;
                        s.logged_in = true;
                        s.last_error = None;
                        drop(s);
                        emit_status(&app, &state).await;
                        // Fall through to the poll interval; the next tick
                        // claims with the fresh cookie.
                    }
                    Err(e) => {
                        log::warn!("re-login failed: {e}");
                        let mut s = state.status.lock().await;
                        s.logged_in = false;
                        s.last_error = Some(format!("signed out, reconnecting: {e}"));
                        drop(s);
                        emit_status(&app, &state).await;
                    }
                }
                tokio::select! {
                    _ = tokio::time::sleep(Duration::from_secs(10)) => {}
                    _ = cancel.changed() => break,
                }
            }
            Err(e) => {
                log::warn!("claim error: {e}");
                let mut s = state.status.lock().await;
                s.last_error = Some(format!("claim: {e}"));
                drop(s);
                emit_status(&app, &state).await;
                tokio::select! {
                    _ = tokio::time::sleep(Duration::from_secs(10)) => {}
                    _ = cancel.changed() => break,
                }
            }
            Ok(None) => {
                reauth_attempts = 0;
                // Idle - back off. Also clear any sticky transient
                // error from a prior failed poll (e.g. a 502 during
                // a backend restart) - successful round-trip means
                // the network is fine again.
                {
                    let mut s = state.status.lock().await;
                    if s.last_error.is_some() {
                        s.last_error = None;
                        drop(s);
                        emit_status(&app, &state).await;
                    }
                }
                tokio::select! {
                    _ = tokio::time::sleep(Duration::from_secs(10)) => {}
                    _ = cancel.changed() => break,
                }
            }
            Ok(Some(job)) => {
                reauth_attempts = 0;
                // Successful claim - clear any sticky transient error
                // from earlier polls before processing the job.
                {
                    let mut s = state.status.lock().await;
                    if s.last_error.is_some() {
                        s.last_error = None;
                    }
                }
                process_job(&app, &state, &cfg, cookies_file_path.as_deref(), job).await;

                // Cookies are exported once at worker start, so a session
                // that goes stale mid-run (user re-signs in, Google rotates
                // the session) would otherwise poison every subsequent job
                // until the app is restarted - which is exactly how a whole
                // channel ends up failing silently. After a failure,
                // re-export from the live webview so the next job picks up
                // the current session. Cheap (reads the cookie store) and
                // best-effort.
                let failed = {
                    let s = state.status.lock().await;
                    s.last_error.is_some()
                };
                if failed {
                    if let Some(path) = cookies_file_path.as_deref() {
                        // Read the account list fresh rather than reusing the
                        // one from worker start: a revocation may have signed
                        // an account out since, and re-exporting from the old
                        // list would put its session straight back on disk.
                        let accounts = load_config(&app).accounts;
                        match export_any_account_cookies(&app, &accounts, path).await
                        {
                            Some(n) => log::info!(
                                "refreshed {n} cookies after a failed job"
                            ),
                            None => log::warn!(
                                "no connected account could refresh cookies"
                            ),
                        }
                    }
                }
            }
        }
    }

    log::info!("worker stopping");
    mark_worker_stopped(&app, &state).await;
}

/// Tell the server the job failed, then mirror it into the UI status. The
/// worker loop watches `last_error` to decide whether to re-export cookies
/// before the next job, so every failure path has to set it.
async fn report_job_failure(
    app: &AppHandle,
    state: &Arc<AppData>,
    cfg: &StoredConfig,
    job_id: &str,
    err: String,
) {
    let _ = fail_job(&state.http, &cfg.base_url, job_id, &err).await;
    let mut s = state.status.lock().await;
    s.last_error = Some(err);
    s.current_job_id = None;
    s.current_video_id = None;
    s.current_progress = 0.0;
    drop(s);
    emit_status(app, state).await;
}

/// A 'metadata' job: ask YouTube what this video's record says right now
/// so the server can compare it against what we archived. Nothing is
/// downloaded and nothing is uploaded.
///
/// This is the only upkeep path that reaches private, unlisted and
/// members-only videos - the server's channel-tab enumeration cannot see
/// them at all, but the worker is signed in as the owner. Which is exactly
/// why it is strict: a fetch that half-worked writes NOTHING. Turning "we
/// could not look" into a field change (or a removal) is the worst output
/// this system can produce.
async fn run_metadata_job(
    app: &AppHandle,
    state: &Arc<AppData>,
    cfg: &StoredConfig,
    cookies_file: Option<&std::path::Path>,
    job: &ClaimedJob,
    work_dir: &std::path::Path,
) {
    // Same cookies the download path uses - that session is what makes the
    // owner's private videos visible in the first place. run_ytdlp's
    // retry-without-cookies fallback still fires underneath, but unlike a
    // download job we refuse the result of it (see below).
    let page_id = load_config(app).channel_page_ids.get(&job.channel_id).cloned();
    let outcome = match run_ytdlp(
        app,
        &job.youtube_url,
        work_dir,
        cookies_file,
        // Metadata only. Never pull the file: there is no upload slot for
        // this kind and the user is not paying bandwidth for a re-read.
        true,
        &job.max_resolution,
        &job.codec_preference,
        // Captions have their own job kind; nothing here consumes them.
        false,
        // Comments have their own kind too; a metadata read skips them.
        false,
        page_id.as_deref(),
    )
    .await
    {
        Ok(o) => o,
        Err(e) => {
            log::warn!("metadata fetch failed for {}: {e}", job.video_id);
            report_job_failure(app, state, cfg, &job.id, e).await;
            return;
        }
    };

    // The cookie'd run failed and the signed-out retry is what answered.
    // Good enough to archive a FILE with, not good enough to version a
    // record against: we set out to read this video as its owner and got a
    // stranger's view instead, and the server has no way to tell the two
    // apart. Fail instead - the post-failure cookie re-export refreshes the
    // session, and the next pass reads it properly.
    if cookies_file.is_some() && outcome.anonymous_fallback {
        let e = "metadata read signed out, wrote nothing: the YouTube session \
                 was stale, so this is not the owner's view of the video"
            .to_string();
        log::warn!("{e} ({})", job.video_id);
        report_job_failure(app, state, cfg, &job.id, e).await;
        return;
    }

    let snapshot = match metadata_snapshot(outcome) {
        Ok(s) => s,
        Err(missing) => {
            // Report every gap at once so the server-side error text says
            // exactly which field the extraction dropped.
            let e = format!(
                "metadata incomplete, wrote nothing: yt-dlp did not report {}",
                missing.join(", ")
            );
            log::warn!("{} for {}", e, job.video_id);
            report_job_failure(app, state, cfg, &job.id, e).await;
            return;
        }
    };

    log::info!(
        "metadata for {}: privacy={:?} views={:?} tags={}",
        job.video_id,
        snapshot.privacy,
        snapshot.view_count,
        snapshot.tags.len()
    );

    let body = MetadataCompletion { metadata: snapshot };
    if let Err(e) = complete_job(&state.http, &cfg.base_url, &job.id, &body).await {
        log::warn!("metadata complete failed for {}: {e}", job.video_id);
        let mut s = state.status.lock().await;
        s.last_error = Some(e);
        s.current_job_id = None;
        s.current_video_id = None;
        s.current_progress = 0.0;
        drop(s);
        emit_status(app, state).await;
        return;
    }

    log::info!("metadata job {} done", job.id);
    let mut s = state.status.lock().await;
    s.current_job_id = None;
    s.current_video_id = None;
    s.current_progress = 0.0;
    // Deliberately NOT counted in completed_count / last_completed_video_id:
    // those mean "videos archived", and a metadata re-read archived nothing.
    drop(s);
    emit_status(app, state).await;
}

/// Render approximate unix SECONDS as an ISO-8601 UTC timestamp with a
/// trailing Z (e.g. "2026-07-04T12:34:56Z"), matching the string the
/// YouTube Data API hands the comment store. Pure civil-date arithmetic so
/// we pull in no chrono/time crate for one format call. The days-to-y/m/d
/// step is Howard Hinnant's civil_from_days; div/rem_euclid keep the time
/// fields non-negative. A YouTube comment never predates 1970, but the math
/// is correct for negative timestamps regardless.
fn unix_to_iso8601(ts: i64) -> String {
    let days = ts.div_euclid(86_400);
    let secs = ts.rem_euclid(86_400);
    let (hour, minute, second) = (secs / 3600, (secs % 3600) / 60, secs % 60);
    // Shift the epoch to 0000-03-01 so leap days land at the end of each
    // 400-year era, then unwind era -> year-of-era -> day-of-year.
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let year = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let day = doy - (153 * mp + 2) / 5 + 1; // [1, 31]
    let month = if mp < 10 { mp + 3 } else { mp - 9 }; // [1, 12]
    let year = year + if month <= 2 { 1 } else { 0 };
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
        year, month, day, hour, minute, second
    )
}

/// A 'comments' job: fetch the video's full comment thread with yt-dlp so
/// the server can diff it against what we archived and surface the ones
/// that were newly deleted. Like a metadata job it pulls no file and
/// uploads nothing - the completion body carries the comment snapshot.
///
/// The one output this feature must never produce is a false "your comment
/// was deleted". yt-dlp returns a SNAPSHOT, never a diff, and can exit 0
/// with a truncated subset (a bot-check/consent interstitial) or a
/// stranger's anonymous view of a private video. So the worker certifies
/// each fetch and the backend only deletes against one marked `complete`.
/// Two of the four safety guards live HERE: we REFUSE an anonymous fallback
/// outright (guard 1), and we set `complete` true only when the owner's
/// cookies actually carried the run (guard 2). The backend owns the sanity
/// ratio (guard 3) and the last_seen_at debounce (guard 4).
async fn run_comment_job(
    app: &AppHandle,
    state: &Arc<AppData>,
    cfg: &StoredConfig,
    cookies_file: Option<&std::path::Path>,
    job: &ClaimedJob,
    work_dir: &std::path::Path,
) {
    // Same cookies the download/metadata paths use - that session is what
    // makes the owner's own comments on a private video visible at all.
    let page_id = load_config(app).channel_page_ids.get(&job.channel_id).cloned();
    let outcome = match run_ytdlp(
        app,
        &job.youtube_url,
        work_dir,
        cookies_file,
        // Re-read only: never pull the file for a comments job.
        true,
        &job.max_resolution,
        &job.codec_preference,
        // Captions are a different kind; this run does not want subs.
        false,
        // Pull the whole comment thread (uncapped - see run_ytdlp).
        true,
        page_id.as_deref(),
    )
    .await
    {
        Ok(o) => o,
        Err(e) => {
            log::warn!("comment fetch failed for {}: {e}", job.video_id);
            report_job_failure(app, state, cfg, &job.id, e).await;
            return;
        }
    };

    // GUARD 1 - OWNER-AUTH (ship-blocking). The cookie'd run failed and the
    // signed-out retry is what answered. An anonymous read of a private
    // video is empty, and of a public one is a stranger's view; either way
    // it is NOT the owner's thread, and letting it reach the diff would mark
    // every real comment "missing" and soft-delete it. Fail instead - the
    // post-failure cookie re-export refreshes the session and the next pass
    // reads it properly. Mirrors run_metadata_job's refusal exactly.
    if cookies_file.is_some() && outcome.anonymous_fallback {
        let e = "comment read signed out, wrote nothing: the YouTube session \
                 was stale, so this is not the owner's view of the thread"
            .to_string();
        log::warn!("{e} ({})", job.video_id);
        report_job_failure(app, state, cfg, &job.id, e).await;
        return;
    }

    // GUARD 2 - COMPLETENESS. Only a fetch the owner's cookies actually
    // carried may certify completeness, which is the sole precondition the
    // backend needs before it will diff-and-delete. We impose no comment cap
    // and run_ytdlp returns Ok only on a yt-dlp exit 0, so "uncapped" and
    // "exit 0" always hold here; the cookie test is the part that varies. A
    // run with no cookies at all (cookies_file None) stays complete=false,
    // so it may insert/update but never delete.
    let complete = cookies_file.is_some() && !outcome.anonymous_fallback;

    let reported_total = outcome.comment_count.unwrap_or(0);
    // None means yt-dlp wrote no `comments` array - comments disabled, or the
    // reader dropped a malformed thread whole on purpose. An empty thread is
    // a legitimate COMPLETE result (that is how "all comments deleted" would
    // look); the backend's empty-snapshot short-circuit means empty items
    // never trigger a deletion, so this is safe either way.
    let items: Vec<CommentItem> = outcome
        .comments
        .unwrap_or_default()
        .into_iter()
        .map(|c| CommentItem {
            id: c.id,
            // yt-dlp's "root" sentinel means top-level; the store wants null
            // there and the parent comment's id on a reply.
            parent_id: if c.parent == "root" { None } else { Some(c.parent) },
            author: c.author.unwrap_or_default(),
            // author_id is the commenter's channel id; the store compares it
            // to the channel owner to resolve is_by_uploader.
            author_channel_id: c.author_id.unwrap_or_default(),
            text: c.text.unwrap_or_default(),
            like_count: c.like_count.unwrap_or(0),
            is_edited: false,
            viewer_rating_like: false,
            published_at: c.timestamp.map(unix_to_iso8601),
            updated_at: None,
        })
        .collect();

    log::info!(
        "comments for {}: fetched={} reported_total={} complete={}",
        job.video_id,
        items.len(),
        reported_total,
        complete
    );

    let body = CommentsCompletion {
        comments: CommentsPayload {
            complete,
            reported_total,
            items,
        },
    };
    if let Err(e) = complete_job(&state.http, &cfg.base_url, &job.id, &body).await {
        log::warn!("comments complete failed for {}: {e}", job.video_id);
        let mut s = state.status.lock().await;
        s.last_error = Some(e);
        s.current_job_id = None;
        s.current_video_id = None;
        s.current_progress = 0.0;
        drop(s);
        emit_status(app, state).await;
        return;
    }

    log::info!("comments job {} done", job.id);
    let mut s = state.status.lock().await;
    s.current_job_id = None;
    s.current_video_id = None;
    s.current_progress = 0.0;
    // Not counted in completed_count / last_completed_video_id: like a
    // metadata re-read, a comments pass archived no video.
    drop(s);
    emit_status(app, state).await;
}

async fn process_job(
    app: &AppHandle,
    state: &Arc<AppData>,
    cfg: &StoredConfig,
    cookies_file: Option<&std::path::Path>,
    job: ClaimedJob,
) {
    // Held for the whole of process_job; dropped on every exit path.
    let _keepalive = spawn_heartbeat(state, cfg.base_url.clone(), job.id.clone());
    {
        let mut s = state.status.lock().await;
        s.current_job_id = Some(job.id.clone());
        s.current_video_id = Some(job.video_id.clone());
        s.current_progress = 0.0;
        s.last_error = None;
    }
    emit_status(app, state).await;

    log::info!(
        "processing {} job {} for video {}",
        job.kind,
        job.id,
        job.video_id
    );

    let tmp = match tempfile::tempdir() {
        Ok(t) => t,
        Err(e) => {
            // Through report_job_failure like every other exit here: it is
            // the one path that also clears current_job_id, and a bare
            // return left the tray saying "Syncing..." about a job that
            // ended before it began.
            report_job_failure(app, state, cfg, &job.id, format!("tempdir: {e}")).await;
            return;
        }
    };

    // Metadata jobs never touch the file or any upload slot, and their
    // completion body is a record snapshot instead of a file report, so
    // they run their own path from here.
    if job.kind == "metadata" {
        run_metadata_job(app, state, cfg, cookies_file, &job, tmp.path()).await;
        return;
    }

    // Comments jobs, like metadata, download and upload nothing: they fetch
    // the thread with --skip-download and their completion body is a comment
    // snapshot, not a file report, so they run their own path from here.
    if job.kind == "comments" {
        run_comment_job(app, state, cfg, cookies_file, &job, tmp.path()).await;
        return;
    }

    // Only a 'video' job wants the file itself. Written as an allow-list
    // rather than a "not captions" test so a kind we don't recognize yet
    // fails SAFE: an unknown kind from a newer server would otherwise burn
    // the user's bandwidth on a full download and then PUT it to an upload
    // url the server never minted for it.
    let skip_download = match job.kind.as_str() {
        "video" => false,
        "captions" => true,
        // A kind only a newer server knows about. Say so and stop. Falling
        // through to the file path would post a normal completion for it,
        // and the server would record a job as DONE that this build never
        // understood - a silent false success is worse than a loud failure,
        // and the error text is how we find out a rebuild is overdue.
        other => {
            let e = format!(
                "this worker build does not handle '{other}' jobs; update the app"
            );
            log::warn!("{e} (job {})", job.id);
            report_job_failure(app, state, cfg, &job.id, e).await;
            return;
        }
    };

    // Run yt-dlp. For 'captions' kind we pass --skip-download so it
    // only writes the .vtt files; for 'video' kind we get back both
    // the mp4 and any captions that happen to exist on the video.
    let page_id = load_config(app).channel_page_ids.get(&job.channel_id).cloned();
    let YtdlpOutcome { mp4: mp4_opt, captions, availability, upload_date, thumbnail: thumbnail_opt, description, tags, view_count, duration_sec, title: _, thumbnail_url: _, comments: _, comment_count: _, anonymous_fallback: _ } = match run_ytdlp(
        app,
        &job.youtube_url,
        tmp.path(),
        cookies_file,
        skip_download,
        &job.max_resolution,
        &job.codec_preference,
        // A captions-kind job exists purely to fetch captions, so it always
        // needs the caption flags; otherwise honor the channel's toggle.
        job.save_captions || skip_download,
        // Video/captions jobs never want the comment thread.
        false,
        page_id.as_deref(),
    )
    .await
    {
        Ok(pair) => pair,
        Err(e) => {
            log::warn!("yt-dlp failed for {}: {e}", job.video_id);
            report_job_failure(app, state, cfg, &job.id, e).await;
            return;
        }
    };

    {
        let mut s = state.status.lock().await;
        s.current_progress = 0.5;
    }
    emit_status(app, state).await;
    let _ = heartbeat(&state.http, &cfg.base_url, &job.id, 0.5).await;

    // Probe + upload the mp4 (video-kind jobs only). Captions-kind
    // jobs never produce one, so we leave the meta block empty for
    // those and skip the R2 PUT entirely.
    let mut meta = FileMeta::default();
    if let Some(mp4) = mp4_opt.as_ref() {
        meta = probe_mp4(app, mp4).await;
        meta.sha256 = hash_file_sha256(mp4).await;
        log::info!(
            "probed {}: {}x? codec={:?} bitrate={:?} fps={:?} audio={:?} hash={}",
            job.video_id,
            meta.video_resolution.as_deref().unwrap_or("?"),
            meta.video_codec.as_deref().unwrap_or("?"),
            meta.video_bitrate_kbps,
            meta.video_fps,
            meta.audio_codec.as_deref().unwrap_or("?"),
            meta.sha256.as_deref().map(|h| &h[..8]).unwrap_or("?"),
        );

        let content_type = job
            .upload_content_type
            .as_deref()
            .unwrap_or("video/mp4");
        if let Err(e) = upload_to_r2(&state.http, &job.upload_url, content_type, mp4).await {
            log::warn!("upload failed for {}: {e}", job.video_id);
            report_job_failure(app, state, cfg, &job.id, e).await;
            return;
        }
    }

    // Upload the poster frame to its presigned slot. Best-effort: a missing
    // thumbnail should never fail an otherwise-good archive, it just means
    // the card renders without art.
    if let Some(thumb) = thumbnail_opt.as_deref() {
        if !job.thumbnail_upload_url.is_empty() {
            match upload_to_r2(
                &state.http, &job.thumbnail_upload_url, "image/jpeg", thumb,
            )
            .await
            {
                Ok(()) => {
                    meta.thumbnail_bytes =
                        std::fs::metadata(thumb).ok().map(|m| m.len());
                    log::info!("uploaded thumbnail for {}", job.video_id);
                }
                Err(e) => log::warn!(
                    "thumbnail upload failed for {}: {e}", job.video_id
                ),
            }
        }
    }

    // Report the real privacy (yt-dlp `availability`). MUST come after the
    // probe block above, which reassigns `meta` wholesale and would
    // otherwise wipe this. The backend only acts on it for video-kind
    // completions.
    meta.availability = availability;
    meta.upload_date = upload_date;
    meta.description = description;
    meta.tags = tags;
    meta.view_count = view_count;
    meta.duration_sec = duration_sec;

    // Upload caption tracks one at a time. We tolerate per-track
    // failures (log + skip) rather than fail the whole job - the user
    // would rather have the video + 4 out of 5 captions archived than
    // nothing. Only languages that landed in R2 successfully get
    // reported back in caption_languages.
    let mut uploaded_langs: Vec<String> = Vec::new();
    for cap in &captions {
        match upload_caption(&state.http, &cfg.base_url, &job.id, cap).await {
            Ok(()) => {
                log::info!(
                    "uploaded caption {}/{}",
                    job.video_id,
                    cap.language
                );
                uploaded_langs.push(cap.language.clone());
            }
            Err(e) => {
                log::warn!(
                    "caption upload failed {}/{}: {e}",
                    job.video_id,
                    cap.language
                );
            }
        }
    }
    // Always send caption_languages, even when empty - that's how the
    // server tells "no manual captions exist" apart from "we never
    // looked". Sort for stable ordering in the metadata.json blob.
    uploaded_langs.sort();
    meta.caption_languages = Some(uploaded_langs);

    {
        let mut s = state.status.lock().await;
        s.current_progress = 0.99;
    }
    emit_status(app, state).await;

    // Mark complete - includes the probed metadata so the website's
    // panel can show resolution / codec / fps / audio / hash. Captions
    // block (caption_languages) is always present.
    if let Err(e) = complete_job(&state.http, &cfg.base_url, &job.id, &meta).await {
        log::warn!("complete failed for {}: {e}", job.video_id);
        // No fail_job here on purpose - the file is in storage and the
        // server's own claim timeout re-queues the job. But the work IS
        // over, so clear it: leaving current_job_id set is how the tray
        // ends up reporting a sync that stopped minutes ago.
        let mut s = state.status.lock().await;
        s.last_error = Some(e);
        s.current_job_id = None;
        s.current_video_id = None;
        s.current_progress = 0.0;
        drop(s);
        emit_status(app, state).await;
        return;
    }

    log::info!("job {} done", job.id);
    {
        let mut s = state.status.lock().await;
        s.current_job_id = None;
        s.current_video_id = None;
        s.current_progress = 0.0;
        s.last_completed_video_id = Some(job.video_id.clone());
        s.completed_count += 1;
    }
    emit_status(app, state).await;
}

// ---------- Tauri commands ----------

#[tauri::command]
async fn save_credentials(
    app: AppHandle,
    base_url: String,
    username: String,
    password: String,
) -> Result<(), String> {
    let prior = load_config(&app);
    let cfg = StoredConfig {
        base_url,
        username,
        password,
        // Preserve connected YouTube accounts across a credential save.
        accounts: prior.accounts,
        // Same for the launch-at-login decision: saving credentials is not
        // the user changing their mind about autostart.
        autostart_declined: prior.autostart_declined,
        // Proof of channel access outlives a credential change too.
        proven_channels: prior.proven_channels,
        linked_channels: prior.linked_channels,
        channel_page_ids: prior.channel_page_ids,
    };
    save_config(&app, &cfg)?;
    Ok(())
}

#[tauri::command]
async fn get_credentials(app: AppHandle) -> StoredConfig {
    load_config(&app)
}

/// Record what the user chose for launch-at-login. Called with true when
/// they switch the toggle OFF and false when they switch it back on, so the
/// enable-by-default pass can tell "never asked" apart from "asked, said
/// no" and never re-enables what somebody deliberately turned off.
#[tauri::command]
async fn set_autostart_declined(app: AppHandle, declined: bool) -> Result<(), String> {
    let mut cfg = load_config(&app);
    if cfg.autostart_declined == declined {
        return Ok(());
    }
    cfg.autostart_declined = declined;
    save_config(&app, &cfg)
}

#[tauri::command]
async fn signin_now(
    app: AppHandle,
    state: State<'_, Arc<AppData>>,
    username: String,
    password: String,
) -> Result<(), String> {
    // Persist the creds first so they survive an app restart, then
    // call /api/auth/login synchronously. The session cookie lands in
    // the shared cookie jar on the reqwest client; subsequent worker
    // requests reuse it. Server creates a UserSession row that shows
    // up in the website's Settings -> Sessions panel.
    let prior = load_config(&app);
    let cfg = StoredConfig {
        base_url: prior.base_url.clone(),
        username: username.clone(),
        password: password.clone(),
        // Preserve connected YouTube accounts across sign-in.
        accounts: prior.accounts.clone(),
        // And the launch-at-login decision - signing in is not a change of
        // mind about autostart.
        autostart_declined: prior.autostart_declined,
        proven_channels: prior.proven_channels.clone(),
        linked_channels: prior.linked_channels.clone(),
        channel_page_ids: prior.channel_page_ids.clone(),
    };
    save_config(&app, &cfg)?;

    login_request(&state.http, &cfg.base_url, &username, &password).await?;

    let mut s = state.status.lock().await;
    s.logged_in = true;
    s.last_error = None;
    drop(s);
    emit_status(&app, &state.inner().clone()).await;
    Ok(())
}

#[tauri::command]
async fn signout_now(
    app: AppHandle,
    state: State<'_, Arc<AppData>>,
) -> Result<(), String> {
    // Stop the worker loop first if it's running - otherwise it'd
    // keep polling /sync-jobs/claim with a soon-to-be-dead cookie.
    {
        let mut g = state.cancel_tx.lock().await;
        if let Some(tx) = g.take() {
            let _ = tx.send(true);
        }
    }

    let cfg = load_config(&app);
    let url = format!(
        "{}/api/auth/logout",
        cfg.base_url.trim_end_matches('/')
    );
    // Best-effort. The server-side session row going away is the
    // important part; even if the request fails (offline, etc.) we
    // still clear the local logged_in flag so the UI matches user
    // intent. Server-side row will time out on its own.
    let _ = state.http.post(&url).send().await;

    // Forget the stored ARCHIVE336 login. Signing out used to only flip
    // the in-memory logged_in flag and leave username + password on disk,
    // so the setup checklist kept reporting "Account credentials saved" -
    // truthfully, since they WERE still saved, just about the wrong thing.
    // Sign out means forget me: clear the credentials so the checklist
    // returns to its pending state and the next sign-in re-enters them.
    //
    // The YouTube account cookies and the launch-at-login choice are
    // deliberately kept. They are device state, not the identity the user
    // just signed out of - and a Google re-auth is expensive enough that
    // bundling it into every sign-out would punish a routine action. The
    // Connections tab has its own per-account Disconnect for those.
    let mut cfg = load_config(&app);
    cfg.username = String::new();
    cfg.password = String::new();
    // Also drop the local sign-in guard so signing back in re-fires the
    // auto-start-sync path rather than sitting idle behind a stale ref.
    let _ = save_config(&app, &cfg);

    let mut s = state.status.lock().await;
    s.logged_in = false;
    s.running = false;
    s.last_error = None;
    drop(s);
    emit_status(&app, &state.inner().clone()).await;
    Ok(())
}

/// How many times a panicking worker loop gets restarted before we stop
/// and say so. Bounded: a panic that reproduces every pass is a bug, and
/// spinning on it would burn the machine instead of surfacing it.
const MAX_WORKER_RESTARTS: u32 = 5;
const WORKER_RESTART_BACKOFF_START: Duration = Duration::from_secs(5);
const WORKER_RESTART_BACKOFF_MAX: Duration = Duration::from_secs(120);

/// Core "start the worker loop" logic, callable from the Tauri command and the
/// tray menu. Errors (already running / not signed in) are surfaced to the
/// command caller and ignored by the tray.
async fn do_start_worker(app: &AppHandle, state: &Arc<AppData>) -> Result<(), String> {
    {
        let s = state.status.lock().await;
        if s.running {
            return Err("worker already running".into());
        }
    }
    let cfg = load_config(app);
    if cfg.username.is_empty() || cfg.password.is_empty() {
        return Err("Sign in first.".into());
    }

    let (tx, rx) = watch::channel(false);
    {
        let mut g = state.cancel_tx.lock().await;
        *g = Some(tx);
    }
    {
        let mut s = state.status.lock().await;
        s.running = true;
        s.last_error = None;
    }
    emit_status(app, state).await;

    let app_clone = app.clone();
    let state_clone = state.clone();
    tokio::spawn(async move {
        // Supervise the loop rather than dropping its JoinHandle. A panic
        // inside worker_loop used to vanish whole: the task died, `running`
        // stayed true, and the tray kept telling the user their channels
        // were being backed up while nothing was running at all. Now a
        // crash either restarts (bounded, with backoff, so a deterministic
        // panic cannot spin) or clears `running` and says what happened.
        let mut restarts: u32 = 0;
        let mut delay = WORKER_RESTART_BACKOFF_START;
        loop {
            let handle = tokio::spawn(worker_loop(
                app_clone.clone(),
                state_clone.clone(),
                cfg.clone(),
                rx.clone(),
            ));
            match handle.await {
                // Clean exit - worker_loop already cleared its own state.
                Ok(()) => break,
                Err(e) if e.is_panic() => {
                    restarts += 1;
                    log::error!("worker loop panicked (restart {restarts}): {e}");
                    // Cancelled mid-panic, or out of restarts: either way we
                    // are not coming back, so stop claiming to be running.
                    let cancelled = *rx.borrow();
                    if restarts > MAX_WORKER_RESTARTS || cancelled {
                        if !cancelled {
                            let mut s = state_clone.status.lock().await;
                            s.last_error = Some(format!(
                                "Worker crashed {restarts} times and stopped. \
                                 Press Start to try again."
                            ));
                        }
                        mark_worker_stopped(&app_clone, &state_clone).await;
                        break;
                    }
                    {
                        let mut s = state_clone.status.lock().await;
                        s.current_job_id = None;
                        s.current_video_id = None;
                        s.current_progress = 0.0;
                        s.last_error = Some(format!(
                            "Worker crashed, restarting in {}s.",
                            delay.as_secs()
                        ));
                    }
                    emit_status(&app_clone, &state_clone).await;
                    tokio::time::sleep(delay).await;
                    delay = (delay * 2).min(WORKER_RESTART_BACKOFF_MAX);
                }
                // The task was cancelled (runtime shutting down). Nothing
                // to restart into.
                Err(e) => {
                    log::warn!("worker loop task ended: {e}");
                    mark_worker_stopped(&app_clone, &state_clone).await;
                    break;
                }
            }
        }
    });

    Ok(())
}

/// Core "stop the worker loop" logic (signals the cancel channel).
async fn do_stop_worker(state: &Arc<AppData>) {
    let mut g = state.cancel_tx.lock().await;
    if let Some(tx) = g.take() {
        let _ = tx.send(true);
    }
}

#[tauri::command]
async fn start_worker(
    app: AppHandle,
    state: State<'_, Arc<AppData>>,
) -> Result<(), String> {
    do_start_worker(&app, state.inner()).await
}

#[tauri::command]
async fn stop_worker(state: State<'_, Arc<AppData>>) -> Result<(), String> {
    do_stop_worker(state.inner()).await;
    Ok(())
}

#[tauri::command]
async fn get_status(state: State<'_, Arc<AppData>>) -> Result<WorkerStatus, String> {
    Ok(state.status.lock().await.clone())
}

#[tauri::command]
async fn ytdlp_check(app: AppHandle) -> Result<String, String> {
    binaries::resolve(&app, "yt-dlp")
        .map(|p| p.display().to_string())
        .ok_or_else(|| "yt-dlp isn't set up yet.".to_string())
}

/// Which managed tools are present right now (drives the setup checklist).
#[tauri::command]
fn binaries_status(app: AppHandle) -> binaries::Status {
    binaries::status(&app)
}


// ---------- Embedded-webview YouTube sessions (multi-account) ----------
//
// Each connected YouTube account gets its OWN isolated WKWebView data
// store (cookie jar), keyed by the account's UUID via
// data_store_identifier — so signing into account B never clobbers
// account A's session. The user signs in once per account via an embedded
// WebviewWindow; cookies persist across restarts. The worker pulls them
// out by spawning a brief hidden webview bound to that account's data
// store, dumping cookies to a Netscape file for yt-dlp's --cookies.
//
// Platform note: data_store_identifier isolation is macOS 14+ / iOS 17+.
// On Windows/Linux it's currently a no-op (single shared store); those
// builds aren't shipping yet, so per-platform isolation lands with them.

/// True for cookies on the domains yt-dlp needs (YouTube + Google auth).
fn is_youtube_cookie(c: &cookie::Cookie<'_>) -> bool {
    let d = c.domain().unwrap_or("");
    d.contains("youtube.com") || d.contains("google.com")
}

/// True when the SAPISID / __Secure-1PSID auth-cookie family is present —
/// what yt-dlp's web_creator client needs for owner-private videos.
fn has_auth_cookies(cookies: &[cookie::Cookie<'static>]) -> bool {
    cookies.iter().any(|c| {
        is_youtube_cookie(c) && (c.name() == "SAPISID" || c.name() == "__Secure-1PSID")
    })
}

/// Build a Cookie header for www.youtube.com requests from webview cookies.
/// Dedupes by name with .youtube.com entries winning over .google.com
/// (some auth cookies exist on both domains with the same name).
fn cookie_header_from(cookies: &[cookie::Cookie<'static>]) -> String {
    let mut map: std::collections::BTreeMap<String, String> = Default::default();
    for pass_youtube in [false, true] {
        for c in cookies {
            let d = c.domain().unwrap_or("");
            let on_youtube = d.contains("youtube.com");
            if (d.contains("google.com") && !pass_youtube) || (on_youtube && pass_youtube) {
                map.insert(c.name().to_string(), c.value().to_string());
            }
        }
    }
    map.iter()
        .map(|(k, v)| format!("{k}={v}"))
        .collect::<Vec<_>>()
        .join("; ")
}

/// Depth-first search a JSON tree for the first occurrence of `key`.
fn json_find<'a>(v: &'a serde_json::Value, key: &str) -> Option<&'a serde_json::Value> {
    match v {
        serde_json::Value::Object(map) => {
            if let Some(found) = map.get(key) {
                return Some(found);
            }
            map.values().find_map(|v| json_find(v, key))
        }
        serde_json::Value::Array(arr) => arr.iter().find_map(|v| json_find(v, key)),
        _ => None,
    }
}

/// Ask YouTube "who is this session?" — returns the ACTIVE channel's
/// display name (e.g. "Afraaz 🗿"). Uses the InnerTube account_menu
/// endpoint authenticated the same way the web client does (SAPISIDHASH
/// derived from the SAPISID cookie). Because it reports the *active*
/// channel, switching to a brand channel inside the sign-in window
/// changes what this returns — which is exactly how we track it.
/// Best-effort: any failure returns None and the UI keeps its fallback.
async fn fetch_active_channel(
    cookies: &[cookie::Cookie<'static>],
) -> Option<(String, Option<String>)> {
    use sha1::{Digest, Sha1};

    let Some(sapisid) = cookies
        .iter()
        .find(|c| c.name() == "SAPISID" || c.name() == "__Secure-3PAPISID")
        .map(|c| c.value().to_string())
    else {
        log::warn!(
            "account_menu: no SAPISID/__Secure-3PAPISID among {} cookies",
            cookies.len()
        );
        return None;
    };
    let origin = "https://www.youtube.com";
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .ok()?
        .as_secs();
    let mut hasher = Sha1::new();
    hasher.update(format!("{ts} {sapisid} {origin}").as_bytes());
    let sash = hex::encode(hasher.finalize());

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(20))
        .user_agent(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 \
             (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        )
        .build()
        .ok()?;
    let res = client
        .post("https://www.youtube.com/youtubei/v1/account/account_menu?prettyPrint=false")
        .header("Authorization", format!("SAPISIDHASH {ts}_{sash}"))
        .header("X-Origin", origin)
        .header("Origin", origin)
        .header("Cookie", cookie_header_from(cookies))
        .json(&serde_json::json!({
            "context": {"client": {"clientName": "WEB", "clientVersion": "2.20250101.00.00"}}
        }))
        .send()
        .await
        .inspect_err(|e| log::warn!("account_menu request failed: {e}"))
        .ok()?;
    if !res.status().is_success() {
        log::warn!("account_menu HTTP {}", res.status());
        return None;
    }
    let data: serde_json::Value = match res.json().await {
        Ok(v) => v,
        Err(e) => {
            // A 200 that is not JSON means an interstitial - consent,
            // bot check, or a login wall - which is a completely
            // different problem from a parse bug, and silently returning
            // None here made the two indistinguishable.
            log::warn!("account_menu returned 200 but unparseable JSON: {e}");
            return None;
        }
    };
    let Some(active) = json_find(&data, "activeAccountHeaderRenderer") else {
        // Names only, never values - this response carries account data.
        log::warn!(
            "account_menu: no activeAccountHeaderRenderer; top-level keys = {:?}",
            data.as_object().map(|o| o.keys().collect::<Vec<_>>())
        );
        return None;
    };
    // accountName has been a simpleText for years, but YouTube migrates
    // these to {runs:[{text}]} renderer by renderer without warning, and
    // a silent None here disables ownership reporting entirely.
    let name = json_find(active, "accountName")
        .and_then(|v| {
            json_find(v, "simpleText")
                .and_then(|s| s.as_str())
                .map(|s| s.to_string())
                .or_else(|| {
                    json_find(v, "runs")
                        .and_then(|r| r.as_array())
                        .map(|runs| {
                            runs.iter()
                                .filter_map(|x| x.get("text").and_then(|t| t.as_str()))
                                .collect::<String>()
                        })
                        .filter(|s| !s.is_empty())
                })
        })
        .or_else(|| {
            log::warn!("account_menu: accountName missing or in an unknown shape");
            None
        })?;
    // The account header's browseId is the login's channel id (UC…),
    // which is proof of which channel this login is signed in as.
    let channel_id = json_find(active, "browseId")
        .and_then(|v| v.as_str())
        .filter(|s| s.starts_with("UC"))
        .map(|s| s.to_string());
    // The success case is worth a line too. Ownership reporting depends
    // entirely on channel_id, and "resolved the name but not the id" is
    // a real outcome that otherwise looks identical to working.
    log::info!(
        "account_menu: signed in as {:?}, channel id {:?}",
        name,
        channel_id
    );
    Some((name, channel_id))
}

/// Parse Apple's `Cookies.binarycookies` format into cookie values.
/// Best-effort: malformed records are skipped, never panics. macOS-only —
/// the format + WebKit store layout are Apple-specific. This is how we read
/// a connected account's session: a cold WKWebView reader on about:blank
/// never loads persisted cookies into memory (WKWebView loads them lazily on
/// navigation), but the bytes are reliably on disk — same source yt-dlp uses
/// for Safari cookies.
#[cfg(target_os = "macos")]
fn parse_binarycookies(bytes: &[u8]) -> Vec<cookie::Cookie<'static>> {
    use cookie::time::OffsetDateTime;
    let mut out = Vec::new();
    if bytes.len() < 8 || &bytes[0..4] != b"cook" {
        return out;
    }
    let be = |b: &[u8], o: usize| -> Option<u32> {
        b.get(o..o + 4)
            .map(|s| u32::from_be_bytes([s[0], s[1], s[2], s[3]]))
    };
    let le = |b: &[u8], o: usize| -> Option<u32> {
        b.get(o..o + 4)
            .map(|s| u32::from_le_bytes([s[0], s[1], s[2], s[3]]))
    };
    let num_pages = match be(bytes, 4) {
        Some(n) => n as usize,
        None => return out,
    };
    let mut sizes = Vec::with_capacity(num_pages);
    for i in 0..num_pages {
        match be(bytes, 8 + 4 * i) {
            Some(s) => sizes.push(s as usize),
            None => return out,
        }
    }
    let mut off = 8 + 4 * num_pages;
    for ps in sizes {
        let page = match bytes.get(off..off + ps) {
            Some(p) => p,
            None => break,
        };
        off += ps;
        let nc = match le(page, 4) {
            Some(n) => n as usize,
            None => continue,
        };
        for i in 0..nc {
            let co = match le(page, 8 + 4 * i) {
                Some(c) => c as usize,
                None => break,
            };
            let c = match page.get(co..) {
                Some(x) => x,
                None => continue,
            };
            let flags = le(c, 8).unwrap_or(0);
            let (do_, no_, po_, vo_) = match (le(c, 16), le(c, 20), le(c, 24), le(c, 28)) {
                (Some(a), Some(b), Some(d), Some(e)) => {
                    (a as usize, b as usize, d as usize, e as usize)
                }
                _ => continue,
            };
            let expiry = c
                .get(40..48)
                .and_then(|s| <[u8; 8]>::try_from(s).ok())
                .map(f64::from_le_bytes)
                .unwrap_or(0.0);
            let cstr = |o: usize| -> Option<String> {
                let s = c.get(o..)?;
                let end = s.iter().position(|&b| b == 0)?;
                std::str::from_utf8(&s[..end]).ok().map(|x| x.to_string())
            };
            let (domain, name, path, value) =
                match (cstr(do_), cstr(no_), cstr(po_), cstr(vo_)) {
                    (Some(d), Some(n), Some(p), Some(v)) => (d, n, p, v),
                    _ => continue,
                };
            if name.is_empty() {
                continue;
            }
            let mut ck = cookie::Cookie::new(name, value);
            ck.set_domain(domain);
            ck.set_path(path);
            ck.set_secure(flags & 1 == 1);
            ck.set_http_only(flags & 4 == 4);
            if expiry > 0.0 {
                // Mac absolute time (since 2001-01-01) → unix epoch.
                let unix = (expiry + 978_307_200.0) as i64;
                if let Ok(dt) = OffsetDateTime::from_unix_timestamp(unix) {
                    ck.set_expires(dt);
                }
            }
            out.push(ck);
        }
    }
    out
}

/// Path to an account store's on-disk cookie file. The WebKit app-name
/// directory isn't predictable, so glob every entry under ~/Library/WebKit.
#[cfg(target_os = "macos")]
fn macos_store_dir(app: &AppHandle, account_id: &str) -> Option<PathBuf> {
    let webkit = app.path().home_dir().ok()?.join("Library").join("WebKit");
    for entry in std::fs::read_dir(&webkit).ok()?.flatten() {
        let dir = entry.path().join("WebsiteDataStore").join(account_id);
        if dir.exists() {
            return Some(dir);
        }
    }
    None
}

/// Read a connected account's cookies straight from its persisted store.
#[cfg(target_os = "macos")]
fn macos_store_cookies(app: &AppHandle, account_id: &str) -> Vec<cookie::Cookie<'static>> {
    let Some(dir) = macos_store_dir(app, account_id) else {
        return Vec::new();
    };
    match std::fs::read(dir.join("Cookies").join("Cookies.binarycookies")) {
        Ok(bytes) => parse_binarycookies(&bytes),
        Err(_) => Vec::new(),
    }
}

/// Permanently wipe an account's store directory on disconnect, so its
/// session leaves nothing behind on disk for the disk-read path to find.
#[cfg(target_os = "macos")]
fn macos_remove_store(app: &AppHandle, account_id: &str) {
    if let Some(dir) = macos_store_dir(app, account_id) {
        let _ = std::fs::remove_dir_all(&dir);
    }
}

/// Read the persisted (window-closed) cookies for an account. macOS reads
/// the on-disk binarycookies deterministically; other platforms (no
/// per-identifier isolation, no binarycookies) fall back to a hidden reader
/// webview on the shared store.
#[cfg(target_os = "macos")]
async fn read_persisted_account_cookies(
    app: &AppHandle,
    account_id: &str,
) -> Result<(Vec<cookie::Cookie<'static>>, Option<tauri::WebviewWindow>), String> {
    Ok((macos_store_cookies(app, account_id), None))
}

#[cfg(not(target_os = "macos"))]
async fn read_persisted_account_cookies(
    app: &AppHandle,
    account_id: &str,
) -> Result<(Vec<cookie::Cookie<'static>>, Option<tauri::WebviewWindow>), String> {
    let label = format!("cookie-reader-{account_id}");
    if let Some(existing) = app.get_webview_window(&label) {
        let _ = existing.close();
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    let url: url::Url = "about:blank"
        .parse()
        .map_err(|e: url::ParseError| e.to_string())?;
    let window = WebviewWindowBuilder::new(app, &label, WebviewUrl::External(url))
        .visible(false)
        .data_store_identifier(account_store_id(account_id))
        .build()
        .map_err(|e| format!("build hidden cookie-reader webview: {e}"))?;
    tokio::time::sleep(std::time::Duration::from_millis(200)).await;
    let cookies = window.cookies().map_err(|e| format!("cookies(): {e}"))?;
    let _ = window.close();
    Ok((cookies, None))
}

/// Read all cookies for `account_id`.
///
/// Prefers the account's live sign-in window when it's open — it has the
/// freshest cookies and reflects an in-progress brand-channel switch, and is
/// returned so the caller can close it once verified. When no window is open,
/// reads the persisted store (deterministic disk read on macOS).
async fn read_account_cookies(
    app: &AppHandle,
    account_id: &str,
) -> Result<(Vec<cookie::Cookie<'static>>, Option<tauri::WebviewWindow>), String> {
    let connect_label = format!("connect-youtube-{account_id}");
    if let Some(win) = app.get_webview_window(&connect_label) {
        let cookies = win.cookies().map_err(|e| format!("cookies(): {e}"))?;
        return Ok((cookies, Some(win)));
    }
    read_persisted_account_cookies(app, account_id).await
}

/// Pull `account_id`'s cookies and write a yt-dlp Netscape cookies.txt to
/// ``out_path``. Returns the cookie count.
///
/// Fails with a user-readable message if no YouTube/Google cookies are
/// present (account not signed in yet) or if the critical
/// SAPISID/__Secure-1PSID auth cookies are missing (session expired).
async fn acquire_cookies_via_webview(
    app: &AppHandle,
    account_id: &str,
    out_path: &std::path::Path,
) -> Result<usize, String> {
    let (cookies, live_window) = read_account_cookies(app, account_id).await?;

    let filtered: Vec<&cookie::Cookie<'static>> = cookies
        .iter()
        .filter(|c| is_youtube_cookie(c))
        .collect();

    if filtered.is_empty() {
        return Err(
            "No YouTube cookies found. Click \"Connect\" and sign in first."
                .to_string(),
        );
    }
    // Sanity check: the SAPISID / __Secure-1PSID family is what
    // yt-dlp's web_creator client needs. Without those, downloads of
    // owner-private videos will fail with "Private video" even
    // though the cookie count looks healthy.
    let has_auth = filtered
        .iter()
        .any(|c| c.name() == "SAPISID" || c.name() == "__Secure-1PSID");
    if !has_auth {
        return Err(
            "YouTube cookies look incomplete (missing SAPISID). Try reconnecting this account.".to_string()
        );
    }

    let body = serialize_webview_cookies_netscape(&filtered);
    if let Some(parent) = out_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    std::fs::write(out_path, body).map_err(|e| format!("write cookies file: {e}"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(
            out_path,
            std::fs::Permissions::from_mode(0o600),
        );
    }
    // The live connect window (when that's where the cookies came from)
    // deliberately stays open: the user may still want YouTube's
    // account-switcher to move onto a brand channel. They close it when
    // they're done; the watcher tracks any switch in the meantime.
    let _ = live_window;
    Ok(filtered.len())
}

/// Tauri command: open the YouTube sign-in webview for an account.
/// `account_id = None` creates a fresh account slot (returning its new
/// id); `Some(id)` reconnects an existing account. The webview is bound
/// to that account's isolated data store so sessions never cross.
#[tauri::command]
async fn connect_youtube_account(
    app: AppHandle,
    account_id: Option<String>,
) -> Result<String, String> {
    let id = match account_id {
        Some(id) => id,
        None => add_account_slot(&app)?,
    };
    let label = format!("connect-youtube-{id}");
    // Close any pre-existing window for this account first.
    if let Some(existing) = app.get_webview_window(&label) {
        let _ = existing.close();
    }
    // Land on youtube.com so the sign-in flow sets the YouTube-domain
    // cookies (LOGIN_INFO / __Secure-YEC / VISITOR_INFO1_LIVE). Logging
    // in via accounts.google.com alone leaves those unset and yt-dlp
    // can't use the SAPISID family without them.
    let url: url::Url = "https://www.youtube.com/"
        .parse()
        .map_err(|e: url::ParseError| e.to_string())?;
    // Dark, because this window is part of our app even though the page
    // inside it is YouTube's. A white 900x700 rectangle opening out of a
    // black app is jarring at 2am, which is when someone is most likely
    // to be setting up a backup.
    //
    // .theme() sets the window's appearance AND the prefers-color-scheme
    // the webview reports, which is the only lever we have over a page we
    // do not control. YouTube honours it for signed-out visitors; once
    // signed in, the account's own theme preference wins and there is
    // nothing further we can or should do about that.
    WebviewWindowBuilder::new(&app, &label, WebviewUrl::External(url))
        .title("Connect YouTube account")
        .inner_size(900.0, 700.0)
        .theme(Some(tauri::Theme::Dark))
        .data_store_identifier(account_store_id(&id))
        .build()
        .map_err(|e| format!("build connect-youtube webview: {e}"))?;

    // Watch the sign-in window for as long as it's open: when the auth
    // cookies appear, read the session's ACTIVE channel name, persist it,
    // and tell the UI — the card flips green with the real channel name,
    // no Re-check needed. Keeps watching after that so switching to a
    // brand channel (YouTube avatar menu → Switch account) updates the
    // card live. Stops when the user closes the window (or after ~10min).
    let watch_app = app.clone();
    let watch_label = label.clone();
    let watch_id = id.clone();
    tauri::async_runtime::spawn(async move {
        let mut announced = false;
        for _ in 0..200 {
            tokio::time::sleep(Duration::from_secs(3)).await;
            let Some(win) = watch_app.get_webview_window(&watch_label) else {
                break;
            };
            let Ok(cookies) = win.cookies() else { continue };
            if !has_auth_cookies(&cookies) {
                continue;
            }
            let title_changed = match fetch_active_channel(&cookies).await {
                Some((t, _)) => set_account_channel_title(&watch_app, &watch_id, &t),
                None => false,
            };
            if !announced || title_changed {
                announced = true;
                let _ = watch_app.emit("youtube-accounts-changed", ());
            }
        }
    });
    Ok(id)
}

/// Every UC channel id anywhere in a JSON blob.
fn collect_channel_ids(v: &serde_json::Value, out: &mut Vec<String>) {
    match v {
        serde_json::Value::String(s) => {
            if s.starts_with("UC") && s.len() >= 20 && !out.iter().any(|x| x == s) {
                out.push(s.clone());
            }
        }
        serde_json::Value::Array(a) => a.iter().for_each(|x| collect_channel_ids(x, out)),
        serde_json::Value::Object(o) => o.values().for_each(|x| collect_channel_ids(x, out)),
        _ => {}
    }
}

/// Outcome of an ownership probe.
///
/// `public_only` is the difference between "we could not get in" and
/// "there was nothing behind the door", which are opposite facts that a
/// bare bool collapsed into the same red error.
#[derive(Serialize, Clone, Debug)]
#[serde(rename_all = "camelCase")]
struct ProbeResult {
    proven: bool,
    public_only: bool,
}

/// The delegated identities ("pageIds") this login can act as.
///
/// Pulled straight out of the accounts_list body with a regex rather
/// than by walking YouTube's renderer tree. The tree shape changes
/// without notice and an earlier attempt to parse it returned nothing
/// at all; the ids themselves are a stable, unambiguous token.
async fn fetch_page_ids(cookies: &[cookie::Cookie<'static>]) -> Vec<String> {
    use sha1::{Digest, Sha1};
    let Some(sapisid) = cookies
        .iter()
        .find(|c| c.name() == "SAPISID" || c.name() == "__Secure-3PAPISID")
        .map(|c| c.value().to_string())
    else {
        return Vec::new();
    };
    let origin = "https://www.youtube.com";
    let Ok(ts) = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
    else {
        return Vec::new();
    };
    let mut hasher = Sha1::new();
    hasher.update(format!("{ts} {sapisid} {origin}").as_bytes());
    let auth = hex::encode(hasher.finalize());
    let Ok(client) = reqwest::Client::builder()
        .timeout(Duration::from_secs(20))
        .user_agent("Mozilla/5.0")
        .build()
    else {
        return Vec::new();
    };
    let res = match client
        .post("https://www.youtube.com/youtubei/v1/account/accounts_list?prettyPrint=false")
        .header("Authorization", format!("SAPISIDHASH {ts}_{auth}"))
        .header("X-Origin", origin)
        .header("Origin", origin)
        .header("Cookie", cookie_header_from(cookies))
        .json(&serde_json::json!({
            "context": {"client": {"clientName": "WEB", "clientVersion": "2.20250101.00.00"}}
        }))
        .send()
        .await
    {
        Ok(r) => r,
        Err(e) => {
            log::warn!("accounts_list failed: {e}");
            return Vec::new();
        }
    };
    let Ok(body) = res.text().await else {
        return Vec::new();
    };
    let mut ids: Vec<String> = Vec::new();
    let needle = "\"pageId\":\"";
    let mut rest = body.as_str();
    while let Some(i) = rest.find(needle) {
        rest = &rest[i + needle.len()..];
        if let Some(end) = rest.find('"') {
            let id = &rest[..end];
            if id.chars().all(|c| c.is_ascii_digit())
                && id.len() >= 15
                && !ids.iter().any(|x| x == id)
            {
                ids.push(id.to_string());
            }
        }
    }
    log::info!("accounts_list: {} delegated identities available", ids.len());
    ids
}

/// Prove this install can reach a channel's private uploads.
///
/// Enumerates the channel's uploads playlist twice - once signed out,
/// once with our cookies - and compares. Anything the signed-in pass
/// sees that the public pass does not is a private or unlisted upload,
/// and only somebody with access to the channel can see those. That is
/// the proof, and it is the capability itself rather than a stand-in
/// for it.
///
/// Why not just ask who we are signed in as: a Google account owns many
/// channels, and picking a different one in YouTube's UI does not change
/// the cookies. It sets a delegated identity carried in a request header
/// we never send, so account_menu answers with the account's PRIMARY
/// channel no matter what is selected. A brand channel could therefore
/// never be authenticated, and our own error message told the user to
/// switch accounts - advice that could not work.
///
/// Deliberately conservative. A channel with no private uploads cannot
/// be proven this way, and returns false. That is correct rather than
/// unfortunate: there is nothing hidden to unlock, so nothing is lost,
/// and identity-based proof still covers the account's own channel.
#[tauri::command]
async fn prove_channel_ownership(
    app: AppHandle,
    youtube_id: String,
    // The slot the user just signed in to. Required in practice, and the
    // reason the first version of this failed: cookies came from
    // export_any_account_cookies, which returns the FIRST slot that
    // yields any. With five slots accumulated the probe tested the OLD
    // account's cookies against the new channel's uploads, saw nothing
    // private, and reported "can't reach it" - a true answer to a
    // question nobody asked.
    account_id: Option<String>,
) -> Result<ProbeResult, String> {
    if !youtube_id.starts_with("UC") || youtube_id.len() < 20 {
        return Err("Not a channel id.".into());
    }
    let uploads = format!("UU{}", &youtube_id[2..]);

    // Getting here means a sign-in completed for this channel. Record
    // that before probing anything: the link is a fact about what the
    // user did, and must not depend on what the probe goes on to find.
    {
        let mut cfg = load_config(&app);
        if !cfg.linked_channels.iter().any(|c| c == &youtube_id) {
            cfg.linked_channels.push(youtube_id.clone());
            save_config(&app, &cfg)?;
        }
    }

    let data_dir = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("app data dir: {e}"))?;
    let cookies_path = data_dir.join("yt-cookies-probe.txt");
    let cookies = match &account_id {
        Some(id) => {
            let n = acquire_cookies_via_webview(&app, id, &cookies_path)
                .await
                .map_err(|e| format!("Couldn't read this sign-in: {e}"))?;
            log::info!("ownership probe using slot {id} ({n} cookies)");
            cookies_path.clone()
        }
        None => acquire_cookies_if_any(&app, &cookies_path)
            .await
            .ok_or_else(|| "Sign in to YouTube in this app first.".to_string())?,
    };

    let public = list_playlist_videos(&app, &uploads, None, None).await;
    let mut signed_in = list_playlist_videos(&app, &uploads, Some(&cookies), None).await;

    // Cookies alone speak as the Google account's PRIMARY channel, so for
    // a brand channel this first pass returns exactly the public set -
    // AFRFX gave 498 of 498 and looked, wrongly, like a channel with
    // nothing private on it.
    //
    // YouTube picks the identity from an X-Goog-PageId header. Try each
    // identity this login can act as and keep the one that returns MORE
    // than the public set: that is both the proof of access and the
    // identity every later request for this channel has to carry. The
    // winning one took AFRFX from 498 to 599.
    //
    // Chosen by result rather than by parsing YouTube's account-switcher
    // structure, which changes shape without notice - an earlier attempt
    // to read names and ids out of that tree returned nothing at all.
    // Always search, never just when the first pass came up empty.
    //
    // The first version only looked for an identity if cookies alone
    // revealed nothing. On AFRFX they revealed exactly one video - a
    // single upload that had been public when we first saw it - so
    // 499 > 498 read as success, the search was skipped, and the other
    // hundred private videos stayed invisible behind a channel the app
    // now called authenticated. A partial answer is the most dangerous
    // kind here, because it looks like a whole one.
    {
        // fetch_page_ids needs the cookie objects, not the exported file.
        let jar = match &account_id {
            Some(id) => read_account_cookies(&app, id).await.ok().map(|(c, _)| c),
            None => None,
        };
        for pid in match &jar {
            Some(c) => fetch_page_ids(c).await,
            None => Vec::new(),
        } {
            let attempt =
                list_playlist_videos(&app, &uploads, Some(&cookies), Some(&pid)).await;
            if attempt.len() > signed_in.len() {
                log::info!(
                    "ownership probe {youtube_id}: identity ...{} reveals {} (was {}, public {})",
                    &pid[pid.len().saturating_sub(6)..],
                    attempt.len(),
                    signed_in.len(),
                    public.len()
                );
                let mut cfg = load_config(&app);
                cfg.channel_page_ids.insert(youtube_id.clone(), pid.clone());
                save_config(&app, &cfg)?;
                signed_in = attempt;
            }
        }
    }
    let _ = std::fs::remove_file(&cookies_path);

    // An empty signed-in pass means the enumeration failed rather than
    // that the channel is empty; treating that as "not owned" would turn
    // a network blip into a revocation.
    if signed_in.is_empty() {
        return Err("Couldn't read that channel's uploads. Try again.".into());
    }

    let ids = |v: &Vec<serde_json::Value>| -> std::collections::HashSet<String> {
        v.iter()
            .filter_map(|e| e.get("id").and_then(|i| i.as_str()).map(String::from))
            .collect()
    };
    let hidden = ids(&signed_in)
        .difference(&ids(&public))
        .count();
    log::info!(
        "ownership probe {youtube_id}: {} signed-in, {} public, {hidden} private",
        signed_in.len(),
        public.len()
    );
    if hidden == 0 {
        // Not a failure. The login saw exactly what the public sees,
        // which for a channel with no private uploads is the only
        // possible outcome - AFRFX returned 498 and 498. Authentication
        // exists to unlock private and members-only videos; a wholly
        // public channel has nothing to unlock, so there is nothing this
        // can prove and nothing the user needs to do. Saying "wrong
        // account" here sends someone to re-authenticate forever over a
        // channel that was already backing up perfectly.
        return Ok(ProbeResult { proven: false, public_only: true });
    }

    let mut cfg = load_config(&app);
    if !cfg.proven_channels.iter().any(|c| c == &youtube_id) {
        cfg.proven_channels.push(youtube_id.clone());
        save_config(&app, &cfg)?;
    }
    Ok(ProbeResult { proven: true, public_only: false })
}

/// Live status of one connected account, surfaced on the Connections tab.
#[derive(Serialize, Debug)]
#[serde(rename_all = "camelCase")]
struct YoutubeAccountStatus {
    id: String,
    /// True when the critical auth cookies are present in this account's
    /// data store and the worker can use them.
    connected: bool,
    /// Count of YouTube+Google cookies found in this account's store.
    cookie_count: usize,
    /// The account's channel name, once known (reported to the website).
    channel_title: Option<String>,
    /// The account's channel id (UC…), proof of which channel this login
    /// is signed in as. Reported to the backend so it records ownership.
    channel_id: Option<String>,
    /// Human-readable message — green-path tagline or the specific reason
    /// connected=false (no cookies, missing auth, etc).
    message: String,
}

/// Probe one account's data store and return its live status. Reads the
/// cookies straight from the store (no temp file) and, when connected,
/// refreshes the channel name from the session — so a brand-channel
/// switch made in the sign-in window shows up on the card.
async fn probe_account(app: &AppHandle, account: &YoutubeAccount) -> YoutubeAccountStatus {
    let mut title = account.channel_title.clone();
    let mut channel_id: Option<String> = None;
    let mk = |connected: bool,
              cookie_count: usize,
              channel_title: Option<String>,
              channel_id: Option<String>,
              message: String| YoutubeAccountStatus {
        id: account.id.clone(),
        connected,
        cookie_count,
        channel_title,
        channel_id,
        message,
    };
    let (cookies, _live) = match read_account_cookies(app, &account.id).await {
        Ok(pair) => pair,
        Err(e) => return mk(false, 0, title, None, e),
    };
    let count = cookies.iter().filter(|c| is_youtube_cookie(c)).count();
    if count == 0 {
        return mk(
            false,
            0,
            title,
            None,
            "No YouTube cookies found. Click \"Connect\" and sign in first.".to_string(),
        );
    }
    if !has_auth_cookies(&cookies) {
        return mk(
            false,
            count,
            title,
            None,
            "YouTube cookies look incomplete (missing SAPISID). Try reconnecting this account."
                .to_string(),
        );
    }
    // Connected — refresh the channel name + id from the live session.
    // Only overwrite the name on success so a transient fetch failure
    // can't blank a known name.
    if let Some((t, cid)) = fetch_active_channel(&cookies).await {
        set_account_channel_title(app, &account.id, &t);
        title = Some(t);
        channel_id = cid;
    }
    let mut st = mk(
        true,
        count,
        title,
        channel_id,
        format!("Connected · {count} cookies"),
    );
    st
}

/// Tauri command: list every connected account with its live status, and
/// One channel the user tracks on the website, as the worker shows it.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct TrackedChannel {
    youtube_id: String,
    title: String,
    handle: String,
    thumbnail_url: String,
    /// Whether THIS channel has been authenticated, not whether some
    /// Google account is signed in. Signing in for one channel says
    /// nothing about another.
    authenticated: bool,
    /// The user withdrew worker access on the website. Distinct from
    /// never-authenticated: this means drop the stored login, not offer
    /// to sign in.
    revoked: bool,
}

/// The channels this worker is allowed to know about.
///
/// The website is the only source. The worker used to discover channels
/// from whatever Google account you signed into, which let you "connect" a
/// channel the website had never heard of - and that did nothing at all,
/// silently, while the app showed green. Asking the server what is tracked
/// makes that state unreachable: no tracked channels means there is
/// genuinely nothing to back up yet, and the fix is on the website.
///
/// Returns an empty list rather than an error when the user is not signed
/// in or the server is unreachable - the UI distinguishes "nothing tracked"
/// from "could not ask" via the Err case, and a transient network blip must
/// not read as "you have no channels".
#[tauri::command]
async fn list_tracked_channels(
    app: AppHandle,
    state: State<'_, Arc<AppData>>,
) -> Result<Vec<TrackedChannel>, String> {
    let cfg = load_config(&app);
    if cfg.username.is_empty() || cfg.password.is_empty() {
        return Err("Not signed in yet.".into());
    }
    let base = cfg.base_url.trim_end_matches('/');
    let url = format!("{base}/api/youtube/worker/tracked-channels");

    let mut res = state.http.get(&url).send().await.map_err(|e| e.to_string())?;
    // A 401 here is routine, not a failure: the session cookie outlives
    // neither a server restart nor a "sign out other devices" click, and
    // the worker is long-lived enough to meet both. Re-auth once and retry
    // rather than telling the user they have no channels.
    if res.status() == reqwest::StatusCode::UNAUTHORIZED {
        login_request(&state.http, &cfg.base_url, &cfg.username, &cfg.password)
            .await
            .map_err(|e| e.to_string())?;
        res = state.http.get(&url).send().await.map_err(|e| e.to_string())?;
    }
    if !res.status().is_success() {
        return Err(format!("Server returned {}.", res.status().as_u16()));
    }
    let body: serde_json::Value = res.json().await.map_err(|e| e.to_string())?;
    let mut channels = body
        .get("channels")
        .and_then(|c| c.as_array())
        .map(|a| {
            a.iter()
                .filter_map(|v| serde_json::from_value::<TrackedChannel>(v.clone()).ok())
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();

    // A completed sign-in shows as authenticated here even when the
    // server has not granted ownership.
    //
    // The two answer different questions. The server's flag means "we
    // proved this login reaches the channel's private videos", which it
    // requires before unlocking them because the video pool is shared
    // and a self-asserted claim would let one subscriber read another's
    // private titles. This app's pill means "a YouTube login is attached
    // to this channel", which is what the Authenticate button asked for
    // and the only thing a sign-in can prove by itself.
    //
    // Without this, signing in correctly left the card still reading
    // "Authenticate" with a paragraph of explanation beside it - the
    // button appearing not to work, and a wall of text where a status
    // belonged.
    let linked = load_config(&app).linked_channels;
    if !linked.is_empty() {
        for ch in channels.iter_mut() {
            if !ch.authenticated && !ch.revoked && linked.contains(&ch.youtube_id) {
                ch.authenticated = true;
            }
        }
    }
    Ok(channels)
}

/// mirror the aggregate up to the backend so the website's Connections
/// tab reflects it. Also the moment a revocation made on the website takes
/// effect here, so the two never disagree once the user looks.
#[tauri::command]
async fn list_youtube_accounts(
    app: AppHandle,
    state: State<'_, Arc<AppData>>,
) -> Result<Vec<YoutubeAccountStatus>, String> {
    Ok(report_and_apply_revocations(&app, state.inner()).await)
}

/// Best-effort: tell the backend our aggregate YouTube connection state so
/// the website's Connections tab can mirror it. Basic users sync via their
/// own worker app, so the backend can't see the embedded-webview cookies
/// otherwise. Requires the worker to be signed in (state.http carries the
/// session); silently skips on any failure.
///
/// Stage 1 reports the aggregate (any account connected, summed cookie
/// count) to the existing single-connection endpoint; per-account
/// mirroring lands with the backend changes.
///
/// Returns the channels the user has revoked on the website
/// ("revokedChannels" in the response), empty on any failure. This function
/// only REPORTS - acting on that list is the caller's job, which is what
/// keeps report -> disconnect -> report from becoming a cycle.
async fn report_youtube_connection_to_backend(
    app: &AppHandle,
    state: &Arc<AppData>,
    statuses: &[YoutubeAccountStatus],
) -> Vec<String> {
    let cfg = load_config(app);
    if cfg.base_url.is_empty() {
        return Vec::new();
    }
    let connected = statuses.iter().any(|s| s.connected);
    let cookie_count: usize = statuses.iter().map(|s| s.cookie_count).sum();
    // The channels our connected logins ARE (their account_menu channel
    // ids). Reported so the backend records ownership and unlocks each
    // channel's sealed videos.
    // Two kinds of proof, either sufficient.
    //
    // Identity: the channel this login IS, per account_menu. Only ever
    // the Google account's primary channel.
    //
    // Capability: channels we have demonstrated we can see private
    // uploads on (see prove_channel_ownership). This is what makes brand
    // channels reachable at all - selecting one in YouTube's UI changes
    // no cookie, so identity alone can never name it.
    let mut owned_channels: Vec<String> = statuses
        .iter()
        .filter(|s| s.connected)
        .filter_map(|s| s.channel_id.clone())
        .collect();
    if connected {
        for c in &cfg.proven_channels {
            if !owned_channels.contains(c) {
                owned_channels.push(c.clone());
            }
        }
    }
    let url = format!(
        "{}/api/youtube/worker-connection",
        cfg.base_url.trim_end_matches('/')
    );
    let body = serde_json::json!({
        "connected": connected,
        "cookieCount": cookie_count,
        "ownedChannels": owned_channels,
    });
    // A 401 here is routine, not fatal, and this is the ONE call that
    // tells the backend which channels our logins own - the call that
    // records ChannelOwnership and unlocks a channel's private videos.
    //
    // It runs on every app launch, where it races the sign-in that
    // establishes the session, and the worker outlives both server
    // restarts and a "sign out other devices" click. It used to log the
    // 401 and give up, so authenticating a channel did nothing at all,
    // no matter how many times the user signed in or switched accounts.
    // Nothing surfaced: the app reported success, the website simply
    // never learned, and the only evidence was one warning line on a
    // stderr stream that Finder-launched apps discard.
    //
    // list_tracked_channels has always re-authenticated and retried on
    // 401. This did not, and the asymmetry was invisible precisely
    // because both failure modes are silent.
    let mut sent = state.http.put(&url).json(&body).send().await;
    let unauthorized =
        matches!(&sent, Ok(r) if r.status() == reqwest::StatusCode::UNAUTHORIZED);
    if unauthorized && !cfg.username.is_empty() && !cfg.password.is_empty() {
        match login_request(&state.http, &cfg.base_url, &cfg.username, &cfg.password).await {
            Ok(()) => {
                log::info!("re-authenticated after 401; retrying connection report");
                sent = state.http.put(&url).json(&body).send().await;
            }
            Err(e) => log::warn!("re-auth before connection report failed: {e}"),
        }
    }
    match sent {
        Ok(r) if r.status().is_success() => r
            .json::<serde_json::Value>()
            .await
            .ok()
            .map(|v| revoked_channels_from(&v))
            .unwrap_or_default(),
        Ok(r) => {
            log::warn!("report youtube connection: HTTP {}", r.status());
            Vec::new()
        }
        Err(e) => {
            log::warn!("report youtube connection failed: {e}");
            Vec::new()
        }
    }
}

/// Pull "revokedChannels" out of a backend response. Both the
/// worker-connection and owned-channels replies carry it; older servers
/// send neither, which reads as "nothing revoked".
fn revoked_channels_from(v: &serde_json::Value) -> Vec<String> {
    v.get("revokedChannels")
        .and_then(|c| c.as_array())
        .map(|a| {
            a.iter()
                .filter_map(|x| x.as_str())
                .filter(|s| s.starts_with("UC"))
                .map(String::from)
                .collect()
        })
        .unwrap_or_default()
}

/// Sign one account out for good: close its windows, wipe its isolated
/// data store (cookies + cache), and drop its slot from config. Other
/// accounts keep their sessions - each has its own store identifier.
async fn wipe_account(app: &AppHandle, account_id: &str) -> Result<(), String> {
    // Close this account's connect window + any stale hidden reader first.
    let connect_label = format!("connect-youtube-{account_id}");
    if let Some(w) = app.get_webview_window(&connect_label) {
        let _ = w.close();
    }
    let reader_label = format!("cookie-reader-{account_id}");
    if let Some(w) = app.get_webview_window(&reader_label) {
        let _ = w.close();
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    // Spawn a hidden webview bound to this account's data store and wipe it.
    let url: url::Url = "about:blank"
        .parse()
        .map_err(|e: url::ParseError| e.to_string())?;
    let window = WebviewWindowBuilder::new(app, &reader_label, WebviewUrl::External(url))
        .visible(false)
        .data_store_identifier(account_store_id(account_id))
        .build()
        .map_err(|e| format!("build hidden webview: {e}"))?;
    tokio::time::sleep(std::time::Duration::from_millis(150)).await;
    let cleared = window.clear_all_browsing_data();
    // Let the async data-store wipe finish before the UI re-checks.
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;
    let _ = window.close();
    cleared.map_err(|e| format!("clear browsing data: {e}"))?;

    remove_account_slot(app, account_id)?;
    // Belt-and-suspenders on macOS: the cold clear_all_browsing_data above
    // doesn't reliably wipe an identifier store, so remove its dir too -
    // otherwise the disk-read path would still see the old session.
    #[cfg(target_os = "macos")]
    macos_remove_store(app, account_id);
    Ok(())
}

/// Probe every connected account and return its live status.
async fn probe_all_accounts(app: &AppHandle) -> Vec<YoutubeAccountStatus> {
    let cfg = load_config(app);
    let mut out = Vec::with_capacity(cfg.accounts.len());
    for acct in &cfg.accounts {
        out.push(probe_account(app, acct).await);
    }
    out
}

/// Reconcile the app with the website: report what we hold, and if the user
/// has revoked a channel there, actually sign that channel's login out here
/// instead of carrying on holding its cookies. Returns the statuses AFTER
/// any disconnect, so the caller shows what is really left.
///
/// Mapping: an account's channel id is the channel its login IS (the
/// account_menu browseId), so a revoked channel identifies the account to
/// drop. An account whose channel id we could not read is LEFT ALONE - with
/// no clean mapping, signing it out could take away a channel the user
/// never revoked, and silently over-disconnecting is worse than being late.
///
/// Non-recursive by construction (this is what stops it looping): a
/// revocation is server-side state that stays true forever, so every report
/// keeps naming that channel. We apply it at most once per call, and the
/// follow-up report deliberately ignores what it gets back. Once the
/// matching account is gone no status carries that channel id, so the next
/// call finds nothing to do and the state settles.
/// Tell the server which revoked channels this machine has finished
/// signing out of. Clearing the server's sticky block is gated on this
/// explicit confirmation - never inferred from a channel missing in a
/// connection report, where a transient probe failure looks identical to
/// a completed sign-out. Failure is only logged: the server keeps naming
/// the channel in revokedChannels until an ack lands, so the next cycle
/// retries this naturally.
async fn acknowledge_revocations(
    state: &Arc<AppData>,
    base_url: &str,
    channels: &[String],
) {
    if channels.is_empty() {
        return;
    }
    let url = format!(
        "{}/api/youtube/worker/revocations/ack",
        base_url.trim_end_matches('/')
    );
    let body = serde_json::json!({ "channels": channels });
    match state.http.post(&url).json(&body).send().await {
        Ok(r) if r.status().is_success() => {
            log::info!("acknowledged revocations: {channels:?}");
        }
        Ok(r) => log::warn!("revocation ack: HTTP {}", r.status()),
        Err(e) => log::warn!("revocation ack failed: {e}"),
    }
}

async fn report_and_apply_revocations(
    app: &AppHandle,
    state: &Arc<AppData>,
) -> Vec<YoutubeAccountStatus> {
    let statuses = probe_all_accounts(app).await;
    let revoked = report_youtube_connection_to_backend(app, state, &statuses).await;
    if revoked.is_empty() {
        return statuses;
    }
    let cfg = load_config(app);
    // Pair each doomed account with the revoked channel it holds, so a
    // successful wipe can be acknowledged for exactly that channel.
    let doomed: Vec<(String, String)> = statuses
        .iter()
        .filter_map(|s| {
            let cid = s.channel_id.as_ref()?;
            revoked
                .iter()
                .any(|r| r == cid)
                .then(|| (s.id.clone(), cid.clone()))
        })
        .collect();
    // Revoked channels no account here holds a session for: the sign-out is
    // already a fact on this machine (done in an earlier cycle, or the login
    // never identified itself), so confirm them straight away. Without this
    // the website's "disconnected" state would wait forever on a wipe that
    // has nothing left to wipe. Caveat, accepted with the simpler model: if
    // another machine still holds the session, this ack clears the block on
    // its behalf.
    let already_gone: Vec<String> = revoked
        .iter()
        .filter(|r| {
            !statuses
                .iter()
                .any(|s| s.channel_id.as_deref() == Some(r.as_str()))
        })
        .cloned()
        .collect();
    acknowledge_revocations(state, &cfg.base_url, &already_gone).await;
    if doomed.is_empty() {
        return statuses;
    }
    let mut wiped_channels: Vec<String> = Vec::new();
    for (id, channel_id) in &doomed {
        match wipe_account(app, id).await {
            Ok(()) => {
                wiped_channels.push(channel_id.clone());
                log::info!(
                    "signed out account {id}: its channel was revoked on the website"
                );
            }
            Err(e) => log::warn!("revoked account {id} could not be signed out: {e}"),
        }
    }
    acknowledge_revocations(state, &cfg.base_url, &wiped_channels).await;
    if wiped_channels.is_empty() {
        // Every wipe failed, so nothing changed. Return quietly rather than
        // announcing a change: the UI reacts to that event by re-listing,
        // which lands back here, and a wipe that keeps failing would spin
        // between the two. The interval re-check retries it instead.
        return statuses;
    }
    // Let the Connections tab redraw from the account list that remains.
    let _ = app.emit("youtube-accounts-changed", ());

    // Re-report so the website and the app agree on what we still hold.
    // The revoked list this call returns is intentionally dropped: the
    // account it names is already gone, and acting on it here is exactly
    // how this would spin.
    let statuses = probe_all_accounts(app).await;
    report_youtube_connection_to_backend(app, state, &statuses).await;
    statuses
}

/// Tauri command: disconnect one account by hand - wipe its isolated data
/// store (cookies + cache) and drop its slot. Other accounts are untouched.
#[tauri::command]
async fn disconnect_youtube_account(
    app: AppHandle,
    state: State<'_, Arc<AppData>>,
    account_id: String,
) -> Result<(), String> {
    wipe_account(&app, &account_id).await?;
    // Re-report the aggregate from what remains. The revoked list this
    // returns is ignored here: the user is disconnecting by hand, and the
    // periodic reconcile is the one place that acts on revocations.
    let statuses = probe_all_accounts(&app).await;
    report_youtube_connection_to_backend(&app, state.inner(), &statuses).await;
    Ok(())
}

/// Serialize a slice of `cookie::Cookie` to Netscape `cookies.txt`
/// format — the format yt-dlp's `--cookies` flag reads.
///
/// Columns (tab-separated): domain · include_subdomains · path · secure
/// · expiry · name · value. Session-only cookies use 0 for expiry.
/// Skips any cookie missing a domain (yt-dlp would reject the line
/// anyway).
fn serialize_webview_cookies_netscape(
    cookies: &[&cookie::Cookie<'static>],
) -> String {
    let mut out = String::from(
        "# Netscape HTTP Cookie File\n# Written by ARCHIVE336\n\n",
    );
    for c in cookies {
        let Some(domain) = c.domain() else { continue };
        let include_subdomains = if domain.starts_with('.') {
            "TRUE"
        } else {
            "FALSE"
        };
        let path = c.path().unwrap_or("/");
        let secure = if c.secure().unwrap_or(false) {
            "TRUE"
        } else {
            "FALSE"
        };
        let expiry: i64 = match c.expires() {
            Some(cookie::Expiration::DateTime(odt)) => odt.unix_timestamp(),
            _ => 0, // session-only or no expiry
        };
        // Skip cookies whose name/value contain tabs — would break
        // the format. Vanishingly rare in practice.
        if c.name().contains('\t') || c.value().contains('\t') {
            continue;
        }
        out.push_str(&format!(
            "{}\t{}\t{}\t{}\t{}\t{}\t{}\n",
            domain,
            include_subdomains,
            path,
            secure,
            expiry,
            c.name(),
            c.value(),
        ));
    }
    out
}

// ---------- System tray / menu bar ----------

/// Bring the main window back: restore the dock icon (macOS), show + focus it.
fn show_main_window(app: &AppHandle) {
    #[cfg(target_os = "macos")]
    {
        let _ = app.set_activation_policy(tauri::ActivationPolicy::Regular);
    }
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.show();
        let _ = win.unminimize();
        let _ = win.set_focus();
    }
}

/// Human-readable tray labels for the current worker state: the status line,
/// an optional "Signed in as …" subline, the contextual toggle text, and
/// whether the toggle is actionable (only once signed in).
fn tray_labels(s: &WorkerStatus, username: &str) -> (String, Option<String>, String, bool) {
    let uname = username.trim();
    let signed_in = s.logged_in || !uname.is_empty();
    let substatus = (!uname.is_empty()).then(|| format!("Signed in as {uname}"));
    let status = if !signed_in {
        "Not signed in".to_string()
    } else if s.running {
        // Order matters, and it is the whole point of this block. The worker
        // now STAYS running while it waits out a problem (no network after a
        // reboot, no connected account, a refused poll), so "running with no
        // current job" is no longer the same thing as "nothing left to do".
        // Reading last_error first is what stops the menu bar - the only
        // surface a background user ever sees - from saying "Up to date"
        // about a machine that is backing nothing up.
        if s.last_error.is_some() {
            "Retrying…".to_string()
        } else if !s.logged_in {
            "Signing in…".to_string()
        } else if s.current_job_id.is_some() {
            "Syncing…".to_string()
        } else {
            "Up to date".to_string()
        }
    } else if s.last_error.is_some() {
        // Not running and something went wrong: the user did not pause this,
        // it stopped, and "Paused" would read as their own doing.
        "Stopped".to_string()
    } else {
        "Paused".to_string()
    };
    let toggle = if s.running {
        "Pause syncing"
    } else {
        "Start syncing"
    }
    .to_string();
    (status, substatus, toggle, signed_in)
}

/// Build the tray menu for a given state. The status header is rendered as
/// disabled items (read-only labels); the single toggle is Pause or Start
/// depending on whether the worker is running.
fn build_tray_menu(
    app: &AppHandle,
    status_text: &str,
    substatus: Option<&str>,
    toggle_text: &str,
    toggle_enabled: bool,
) -> tauri::Result<tauri::menu::Menu<tauri::Wry>> {
    use tauri::menu::{IsMenuItem, Menu, MenuItem, PredefinedMenuItem};

    let status = MenuItem::with_id(app, "tray_status", status_text, false, None::<&str>)?;
    let sub = MenuItem::with_id(
        app,
        "tray_substatus",
        substatus.unwrap_or_default(),
        false,
        None::<&str>,
    )?;
    let toggle = MenuItem::with_id(app, "tray_toggle", toggle_text, toggle_enabled, None::<&str>)?;
    let website = MenuItem::with_id(app, "tray_open_website", "Open Website", true, None::<&str>)?;
    let app_win = MenuItem::with_id(app, "tray_open_app", "Open Worker App", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "tray_quit", "Quit ARCHIVE336", true, None::<&str>)?;
    let sep1 = PredefinedMenuItem::separator(app)?;
    let sep2 = PredefinedMenuItem::separator(app)?;
    let sep3 = PredefinedMenuItem::separator(app)?;

    let mut items: Vec<&dyn IsMenuItem<tauri::Wry>> = vec![&status];
    if substatus.is_some() {
        items.push(&sub);
    }
    items.push(&sep1);
    items.push(&toggle);
    items.push(&sep2);
    items.push(&website);
    items.push(&app_win);
    items.push(&sep3);
    items.push(&quit);

    Menu::with_items(app, &items)
}

/// Rebuild + swap the tray menu to reflect `s`. Menu mutation must happen on
/// the main thread on macOS, so `refresh_tray` hops there before calling this.
fn set_tray_menu(app: &AppHandle, s: &WorkerStatus, username: &str) {
    let (status_text, substatus, toggle_text, toggle_enabled) = tray_labels(s, username);
    match build_tray_menu(
        app,
        &status_text,
        substatus.as_deref(),
        &toggle_text,
        toggle_enabled,
    ) {
        Ok(menu) => {
            if let Some(tray) = app.tray_by_id("main-tray") {
                let _ = tray.set_menu(Some(menu));
            }
        }
        Err(e) => log::warn!("tray menu rebuild failed: {e}"),
    }
}

/// Push the current worker state into the menu-bar tray from any thread.
fn refresh_tray(app: &AppHandle, s: &WorkerStatus) {
    let username = load_config(app).username;
    let s = s.clone();
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || set_tray_menu(&handle, &s, &username));
}

/// Build the tray icon (menu bar on macOS, system tray on Windows) with a live
/// status header + controls, so the app can keep running in the background
/// without sitting in the dock. The menu rebuilds on every status change (see
/// emit_status -> refresh_tray).
fn setup_tray(app: &AppHandle) -> tauri::Result<()> {
    use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
    use tauri_plugin_opener::OpenerExt;

    // Seed the menu from what we know at launch (a saved username; worker not
    // yet running). emit_status keeps it live from here on.
    let username = load_config(app).username;
    let seed = WorkerStatus {
        logged_in: !username.trim().is_empty(),
        ..Default::default()
    };
    let (status_text, substatus, toggle_text, toggle_enabled) = tray_labels(&seed, &username);
    let menu = build_tray_menu(
        app,
        &status_text,
        substatus.as_deref(),
        &toggle_text,
        toggle_enabled,
    )?;

    #[allow(unused_mut)]
    let mut builder = TrayIconBuilder::with_id("main-tray")
        .icon(tauri::include_image!("icons/tray.png"))
        .menu(&menu)
        // macOS: left-click opens the menu (platform norm). Windows: left-click
        // shows the window, right-click opens the menu.
        .show_menu_on_left_click(cfg!(target_os = "macos"))
        .on_menu_event(|app, event| match event.id.as_ref() {
            "tray_open_app" => show_main_window(app),
            // One contextual control: pause if running, start if not.
            "tray_toggle" => {
                let app = app.clone();
                tauri::async_runtime::spawn(async move {
                    let state = app.state::<Arc<AppData>>().inner().clone();
                    let running = state.status.lock().await.running;
                    if running {
                        do_stop_worker(&state).await;
                    } else {
                        let _ = do_start_worker(&app, &state).await;
                    }
                });
            }
            "tray_open_website" => {
                let cfg = load_config(app);
                let mut url = cfg.base_url.trim_end_matches('/').to_string();
                if url.is_empty() {
                    url = "https://archive336.com".to_string();
                }
                let _ = app.opener().open_url(url, None::<&str>);
            }
            "tray_quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_main_window(tray.app_handle());
            }
        });

    // macOS menu-bar icons should adapt to the light/dark bar; our tray.png is
    // a transparent logo, so render it as a template.
    #[cfg(target_os = "macos")]
    {
        builder = builder.icon_as_template(true);
    }

    builder.build(app)?;
    Ok(())
}

// ---------- Tauri entry ----------

/// The LaunchAgent this app registered back when its productName was
/// "Aether Archive Tool". Renaming to ARCHIVE336 changed the plist the
/// autostart plugin writes, and left the old one behind - pointing at a
/// binary path that no longer exists. launchd has been retrying it (and
/// failing) at every login since, while the app's own login item is
/// absent, so the machine looks configured and backs up nothing.
///
/// One exact filename, in the user's own LaunchAgents dir, and nothing
/// else: every other plist in there belongs to somebody else.
#[cfg(target_os = "macos")]
fn remove_legacy_launch_agent() {
    let Some(home) = std::env::var_os("HOME") else {
        return;
    };
    let path = PathBuf::from(home)
        .join("Library/LaunchAgents")
        .join("Aether Archive Tool.plist");
    if !path.is_file() {
        return;
    }
    // Unload first so launchd stops retrying in THIS login session too,
    // not just after the next reboot. Best-effort: an agent that never
    // loaded (its binary is missing) makes this exit non-zero, which is
    // fine - the file going away is the part that matters.
    let _ = std::process::Command::new("/bin/launchctl")
        .arg("unload")
        .arg(&path)
        .output();
    match std::fs::remove_file(&path) {
        Ok(()) => log::info!("removed orphaned login item {}", path.display()),
        Err(e) => log::warn!("could not remove {}: {e}", path.display()),
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    env_logger::init();
    let app_data = Arc::new(AppData::new());
    tauri::Builder::default()
        // Single-instance guard: if the app is already running, a second
        // launch just focuses the existing window and exits - so one machine
        // never ends up with multiple workers fighting over jobs. Must be the
        // first plugin registered.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            // Relaunching the app surfaces the existing window (restoring the
            // dock icon on macOS) instead of starting a second copy.
            show_main_window(app);
        }))
        .plugin(tauri_plugin_opener::init())
        // Launch-at-login: registers/removes a LaunchAgent on macOS (and the
        // equivalent on Windows/Linux) when the Settings toggle is flipped.
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            // Launch-at-login starts the worker hidden in the tray (the quiet
            // background-downloader mode); a manual open shows the window.
            Some(vec!["--hidden"]),
        ))
        .plugin(tauri_plugin_deep_link::init())
        .manage(app_data)
        .setup(|app| {
            // Clear the dead pre-rename login item before anything else
            // touches autostart, so the plugin's view of "is launch at
            // login set up" is not competing with a stale plist.
            #[cfg(target_os = "macos")]
            remove_legacy_launch_agent();

            // archive336:// and aether-archive-tool:// links from the
            // website land here. Whatever the path says, the right response
            // is the same: come to the front and show the Connections tab -
            // every link we mint today is about connecting an account, and
            // a wrong-but-visible tab beats a dead click. The webview
            // listens for the event and switches tabs.
            {
                use tauri_plugin_deep_link::DeepLinkExt;
                let handle = app.handle().clone();
                app.deep_link().on_open_url(move |event| {
                    log::info!("deep link: {:?}", event.urls());
                    #[cfg(target_os = "macos")]
                    {
                        // The tray/background mode drops the dock icon and
                        // hides the window; a deep link is an explicit ask
                        // to see the app, so undo both.
                        let _ = handle
                            .set_activation_policy(tauri::ActivationPolicy::Regular);
                    }
                    if let Some(win) = handle.get_webview_window("main") {
                        let _ = win.show();
                        let _ = win.set_focus();
                    }
                    let _ = handle.emit("deep-link-connect", ());
                });
            }
            // On launch, make sure the worker's tools (yt-dlp, ffmpeg,
            // ffprobe) are downloaded and yt-dlp is up to date. Runs in the
            // background so the window opens immediately; the UI listens on
            // the `binaries://progress` event and gates Start on readiness.
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                if let Err(e) = binaries::ensure_all(&handle).await {
                    log::error!("binary setup failed: {e}");
                }
            });

            // Menu-bar / system-tray icon with worker controls.
            setup_tray(app.handle())?;

            // Closing the window hides it to the tray and (on macOS) drops the
            // dock icon, so the worker keeps running in the background.
            if let Some(win) = app.get_webview_window("main") {
                let win_handle = win.clone();
                win.on_window_event(move |event| {
                    if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                        api.prevent_close();
                        let _ = win_handle.hide();
                        #[cfg(target_os = "macos")]
                        {
                            let _ = win_handle
                                .app_handle()
                                .set_activation_policy(tauri::ActivationPolicy::Accessory);
                        }
                    }
                });
            }

            // Launched at login (--hidden)? Stay in the tray instead of
            // popping the window open, and start syncing on its own - the
            // quiet background-downloader mode. A manual open has no --hidden
            // flag and shows the window as usual.
            if std::env::args().any(|a| a == "--hidden") {
                if let Some(win) = app.get_webview_window("main") {
                    let _ = win.hide();
                }
                #[cfg(target_os = "macos")]
                {
                    let _ = app
                        .handle()
                        .set_activation_policy(tauri::ActivationPolicy::Accessory);
                }
                let start_handle = app.handle().clone();
                tauri::async_runtime::spawn(async move {
                    // Give binary setup a head start; the worker loop is
                    // resilient if yt-dlp isn't ready yet (it keeps polling).
                    tokio::time::sleep(Duration::from_secs(3)).await;
                    let state = start_handle.state::<Arc<AppData>>().inner().clone();
                    if let Err(e) = do_start_worker(&start_handle, &state).await {
                        log::info!("background auto-start skipped: {e}");
                    }
                });
            }

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            prove_channel_ownership,
            save_credentials,
            get_credentials,
            set_autostart_declined,
            signin_now,
            signout_now,
            start_worker,
            stop_worker,
            get_status,
            ytdlp_check,
            binaries_status,
            connect_youtube_account,
            list_youtube_accounts,
            list_tracked_channels,
            disconnect_youtube_account,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

