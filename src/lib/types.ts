export type VideoStatus =
  | "discovered"
  | "syncing"
  | "archived"
  | "failed"
  | "deleted_on_youtube"

export type VideoPrivacy = "public" | "unlisted" | "private" | "members"

/**
 * "deleted" is a stored value, never a label. Saved filter presets persist
 * visibilities:["deleted"] in the database, so the value is frozen; the UI
 * shows "Unavailable" instead (see VISIBILITY_LABELS in PresetEditor), because
 * all our detection actually proves is that we could not see the video on
 * YouTube when we last looked.
 */
export type VideoVisibility = VideoPrivacy | "deleted"

export type VideoType = "video" | "short" | "livestream"

export type SortDimension =
  | "upload"
  | "views"
  | "filesize"
  | "duration"

export type SortDirection = "asc" | "desc"

export type FilterPreset = {
  id: string
  label: string
  /** True for the built-in "All" preset: label + filters locked, only sort + view are editable. */
  locked: boolean
  search: string
  visibilities: VideoVisibility[]
  types: VideoType[]
  dateFrom: string
  dateTo: string
  sortDimension: SortDimension
  sortDirection: SortDirection
  viewMode: "grid" | "list"
}

export type ChannelLink = {
  label: string
  url: string
}

export type VideoMaxResolution =
  | "audio-only"
  | "360p"
  | "480p"
  | "720p"
  | "1080p"
  | "1440p"
  | "2160p"
  | "source"

export type CodecPreference = "compat" | "efficient"

export type MetadataRefreshFrequency =
  | "weekly"
  | "monthly"
  | "quarterly"
  | "annually"

export type VideoCardMetaField =
  | "uploadDate"
  | "duration"
  | "fileSize"
  | "type"

export type ChannelArchiveSettings = {
  // Channel state
  /** Master switch. When false, nothing syncs, scrapes, or downloads. */
  active: boolean
  /** When false, new uploads are still discovered but not auto-downloaded. */
  downloadNewVideos: boolean

  // Video download
  maxResolution: VideoMaxResolution
  codecPreference: CodecPreference

  // Per-video elements (video file itself is always saved).
  // Comments are intentionally NOT a per-video metadata toggle - they
  // get their own dedicated top-level sync surface (third option
  // alongside Videos and Metadata) with its own settings. Captions
  // likewise live as their own SyncPanel toggle - they're captured
  // automatically on every video sync, with a manual backfill switch.
  saveThumbnail: boolean
  saveViewCount: boolean
  saveDescription: boolean
  saveTags: boolean
  saveCaptions: boolean
  // Per-field history. When on, each rescan records the prior value with the
  // span it was live, building a change-log (for view count, a time-series).
  // Off keeps only the current value. Requires the matching capture flag.
  saveThumbnailHistory: boolean
  saveViewCountHistory: boolean
  saveDescriptionHistory: boolean
  saveTagsHistory: boolean
  saveCaptionsHistory: boolean
  saveChannelAvatarHistory: boolean
  saveChannelAboutHistory: boolean
  saveChannelStatsHistory: boolean
  // When a new video is auto-synced, also take an initial metadata
  // snapshot using the field toggles below. Off = only the video file
  // gets downloaded, metadata fields stay untracked until the next
  // scheduled metadata-sync run picks them up. On = preserves today's
  // behavior of "capture everything at archive time".
  includeMetadataOnVideoSync: boolean
  metadataRefreshFrequency: MetadataRefreshFrequency

  // Comments sync — separate cadence from metadata. Comments churn
  // much faster than titles/descriptions, but most users don't care
  // about that churn, so the default is manual-only. When syncComments
  // is on we pull the entire comment tree including replies — replies
  // aren't a separate toggle.
  syncComments: boolean
  commentsRefreshFrequency: MetadataRefreshFrequency

  // Channel-level elements
  saveChannelStatsSnapshots: boolean
  saveChannelAvatar: boolean
  saveChannelAbout: boolean

  // Video list display
  showStatusBadges: boolean
  useStatusColorBorder: boolean
  cardMetaFields: VideoCardMetaField[]

  // Filter presets — surfaced as tabs on the channel detail page.
  // Conceptually a global/user-level preference; stored here for now.
  filterPresets: FilterPreset[]

  // Notification emails — which YouTube archive events email the user.
  // Conceptually a per-integration (YouTube-wide) preference; rides in
  // this settings blob the way filterPresets does. Integrity events
  // default on (critical info, not marketing); activity events default
  // off (avoid spamming new users). Every one of these now gates a real
  // send - see backend/app/notify.py and its callers in channel_rescan,
  // metadata_rescan, oauth_loader and scripts/monthly_digest.
  notifyVideoDeleted: boolean
  notifyChannelTerminated: boolean
  notifyOauthDisconnected: boolean
  notifyNewUpload: boolean
  notifyMonthlyDigest: boolean
}

/**
 * What's happening to the channel on YouTube right now. Always defaults to
 * "available" — anything else means YouTube has the channel in some
 * degraded state. We preserve our archive regardless of this value.
 */
export type ChannelYoutubeStatus = "available" | "terminated"

export type Channel = {
  id: string
  handle: string
  name: string
  avatarUrl: string
  description: string
  subscriberCount: number
  videoCount: number
  /** Real count of this channel's videos we've archived (downloaded). */
  archivedVideoCount?: number
  /** Videos we know about that this viewer may see. The denominator of
   *  the card's "archived / total" - NOT videoCount, which is YouTube's
   *  public number and excludes the private videos we archive. */
  knownVideoCount?: number
  /**
   * Whether comment syncing is possible for this channel. Comments come
   * from the YouTube Data API using the channel's OAuth token, so a
   * channel tracked by URL (no connected account) can't sync them.
   */
  commentsSyncAvailable?: boolean
  totalViews: number
  country: string
  joinedAt: string
  links: ChannelLink[]
  addedAt: string
  lastSyncedAt: string
  /** When the channel disappeared from YouTube. Null otherwise. */
  terminatedAt: string | null
  /** Current YouTube-side status. "available" by default. */
  youtubeStatus: ChannelYoutubeStatus
  settings: ChannelArchiveSettings
  /** Projected monthly storage cost for keeping this channel
   * subscribed at its current size. Backend computes from the
   * Video.bytes_stored sum × the user's effective markup × the
   * average month in hours. Null/undefined on legacy payloads. */
  projectedMonthlyCostUsd?: number
  /** The bytes the cost above was computed from - videos plus their
      thumbnails, the same basis billing meters. Shown as the Storage stat
      so storage x advertised rate visibly equals cost. */
  bytesStored?: number
  /** True iff we have an active PubSubHubbub subscription for this
   * channel — meaning new uploads land in seconds rather than the
   * next polling cycle. False = subscription expired or never
   * established; the renewal cron retries daily. */
  pubsubLive?: boolean
  /** True when the user has turned the worker app's authentication for
   * this channel back off. Sealed videos (private, unlisted,
   * members-only) stop being discovered and synced from that moment;
   * nothing already archived is touched. Sticky - a routine worker
   * ownership report does not clear it, only an explicit re-authenticate
   * does. Absent on legacy payloads, so treat undefined as "not revoked". */
  ownershipRevoked?: boolean
  /** Whether the worker app has proven access to this channel's private
   *  videos. Absent on legacy payloads, which read as not authenticated. */
  authenticated?: boolean
}

export type VideoComment = {
  id: string
  author: string
  authorAvatarUrl: string
  text: string
  likes: number
  publishedAt: string
  replyCount: number
}

export type Video = {
  id: string
  channelId: string
  /** Set only by the cross-channel library endpoint (GET /videos).
   *  A list that mixes channels is unreadable without attribution, so
   *  the row carries its own rather than making the client join against
   *  whatever channel list it happens to have loaded. Absent on the
   *  per-channel listing, where the channel is already the context. */
  channelName?: string
  channelHandle?: string
  title: string
  description: string
  uploadDate: string
  durationSec: number
  thumbnailUrl: string
  status: VideoStatus
  privacy: VideoPrivacy
  // Our archive's own access tier, distinct from YouTube's privacy.
  // "open" = any subscriber can watch; "sealed" = owner-only. Frozen at
  // capture, so a video grabbed while public stays open even if the
  // source is later privated. Optional for legacy/mock payloads.
  visibility?: "open" | "sealed"
  type: VideoType
  viewCount: number
  tags: string[]
  commentCount: number
  comments: VideoComment[]
  captionLanguages: string[]
  videoFormat: string | null
  videoResolution: string | null
  videoBitrateKbps: number | null
  // Captured by ffprobe on the worker at sync time. Optional - older
  // syncs (pre-ffprobe-integration) have these as null, and the worker
  // skips fields it couldn't determine.
  videoCodec: string | null
  videoFps: number | null
  audioCodec: string | null
  audioBitrateKbps: number | null
  fileSha256: string | null
  localPath: string | null
  fileSizeBytes: number | null
  firstSeenAt: string
  archivedAt: string | null
  /** maxResolution setting on the channel when this video was archived.
   * Null for pre-feature archives + videos that haven't been synced yet.
   * Compared to the channel's CURRENT maxResolution to detect outdated
   * quality - if the current setting is strictly higher, the video can
   * be re-archived at a better quality. */
  archivedMaxResolution: VideoMaxResolution | null
  /** codecPreference setting at archive time. Same comparison logic. */
  archivedCodecPreference: CodecPreference | null
  lastYoutubeCheckAt: string | null
  deletedOnYoutubeAt: string | null
  syncProgress?: number
}
