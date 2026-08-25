import type {
  Channel,
  ChannelArchiveSettings,
  MetadataRefreshFrequency,
  Video,
  VideoComment,
} from "./types"

// The only refresh cadences we offer now — "manual" was retired (the whole
// service is automatic). Legacy stored "manual" values are coerced to the
// per-field default on load.
const VALID_REFRESH_FREQUENCIES: MetadataRefreshFrequency[] = [
  "weekly",
  "monthly",
  "quarterly",
  "annually",
]

function coerceRefreshFrequency(
  value: unknown,
  fallback: MetadataRefreshFrequency
): MetadataRefreshFrequency {
  return VALID_REFRESH_FREQUENCIES.includes(value as MetadataRefreshFrequency)
    ? (value as MetadataRefreshFrequency)
    : fallback
}

const now = new Date()
const daysAgo = (d: number) => new Date(now.getTime() - d * 86400 * 1000).toISOString()

export const defaultChannelSettings: ChannelArchiveSettings = {
  // Mirrors _DEFAULT_CHANNEL_SETTINGS in backend/app/routes/youtube.py.
  // Active by default: adding a channel backs it up, which cannot be true
  // of a channel that starts paused.
  active: true,
  downloadNewVideos: true,
  maxResolution: "source",
  codecPreference: "compat",
  saveThumbnail: true,
  saveViewCount: true,
  saveDescription: true,
  saveTags: true,
  saveCaptions: true,
  saveThumbnailHistory: true,
  saveViewCountHistory: true,
  saveDescriptionHistory: true,
  saveTagsHistory: true,
  saveCaptionsHistory: true,
  includeMetadataOnVideoSync: true,
  metadataRefreshFrequency: "monthly",
  syncComments: true,
  commentsRefreshFrequency: "monthly",
  saveChannelStatsSnapshots: true,
  saveChannelAvatar: true,
  saveChannelAbout: true,
  saveChannelAvatarHistory: true,
  saveChannelAboutHistory: true,
  saveChannelStatsHistory: true,
  showStatusBadges: true,
  useStatusColorBorder: true,
  cardMetaFields: ["uploadDate", "fileSize", "duration", "type"],
  // Owner's chosen defaults: integrity alerts off. The notification
  // send paths are still being proven, so off avoids promising an email
  // we might not deliver; a user turns them on when they want them.
  notifyVideoDeleted: false,
  notifyChannelTerminated: false,
  notifyOauthDisconnected: false,
  // Activity: off by default (don't spam new users).
  notifyNewUpload: false,
  notifyMonthlyDigest: false,
  filterPresets: [
    {
      id: "all",
      label: "All",
      locked: true,
      search: "",
      visibilities: [],
      types: [],
      dateFrom: "",
      dateTo: "",
      sortDimension: "upload",
      sortDirection: "desc",
      viewMode: "grid",
    },
    {
      id: "archived",
      // "Archived" = held in ARCHIVE336 but not a normal public video on
      // YouTube: private, unlisted, members-only, or gone (deleted). The
      // owner's definition - the collection of everything we keep that the
      // public can't, or can no longer, watch on YouTube. Note this differs
      // from the channel-header "Archived X/Y" stat, which counts every
      // video we hold a copy of, public included.
      label: "Archived",
      locked: false,
      search: "",
      visibilities: ["deleted", "private", "unlisted", "members"],
      types: [],
      dateFrom: "",
      dateTo: "",
      sortDimension: "upload",
      sortDirection: "desc",
      viewMode: "grid",
    },
  ],
}

/**
 * Backfill missing fields on a settings object loaded from the API.
 *
 * Channels created before a new field existed have settings JSON that
 * lacks that key entirely. Reading that key returns undefined, which
 * makes Select components render with no selection and toggles behave
 * unpredictably. Normalize at the load boundary so the rest of the app
 * can treat settings as a complete object.
 */
export function normalizeChannelSettings(
  raw: Partial<ChannelArchiveSettings> | null | undefined
): ChannelArchiveSettings {
  if (!raw) {
    return {
      ...defaultChannelSettings,
      filterPresets: [...defaultChannelSettings.filterPresets],
    }
  }
  return {
    ...defaultChannelSettings,
    ...raw,
    // Coerce retired / unknown refresh cadences (e.g. legacy "manual") to
    // the per-field default so the dropdowns always have a valid selection.
    metadataRefreshFrequency: coerceRefreshFrequency(
      raw.metadataRefreshFrequency,
      defaultChannelSettings.metadataRefreshFrequency
    ),
    commentsRefreshFrequency: coerceRefreshFrequency(
      raw.commentsRefreshFrequency,
      defaultChannelSettings.commentsRefreshFrequency
    ),
    // filterPresets is an array - if it's missing or empty, fall back to the
    // defaults so the user always has at least an "All" view. Saved presets
    // are kept as-is: "Archived" is a deliberate label now (held here, not
    // publicly on YouTube), so there is nothing to heal. Any preset still
    // labelled "Deleted" from the brief first-rename stays as the user saved
    // it rather than being silently rewritten.
    filterPresets:
      Array.isArray(raw.filterPresets) && raw.filterPresets.length > 0
        ? raw.filterPresets
        : [...defaultChannelSettings.filterPresets],
    // cardMetaFields likewise - empty array is a valid user choice, but
    // undefined isn't.
    cardMetaFields: Array.isArray(raw.cardMetaFields)
      ? raw.cardMetaFields
      : defaultChannelSettings.cardMetaFields,
  }
}

export const mockChannels: Channel[] = []

type VideoInput = {
  id: string
  channelId: string
  title: string
  description: string
  daysSinceUpload: number
  durationSec: number
  status: Video["status"]
  archivedDaysAgo?: number
  deletedDaysAgo?: number
  syncProgress?: number
  fileSizeBytes?: number | null
  localPath?: string | null
  viewCount?: number
  privacy?: Video["privacy"]
  type?: Video["type"]
  tags?: string[]
  comments?: VideoComment[]
  commentCount?: number
  captionLanguages?: string[]
}

function seededHash(s: string): number {
  let h = 0
  for (let i = 0; i < s.length; i++) {
    h = (h * 31 + s.charCodeAt(i)) & 0x7fffffff
  }
  return h
}

const TAG_POOL = [
  "science",
  "education",
  "physics",
  "math",
  "history",
  "explainer",
  "documentary",
  "experiment",
  "interview",
  "theory",
  "longform",
  "animation",
  "analysis",
  "research",
  "curiosity",
]

function defaultTagsFor(id: string): string[] {
  const h = seededHash(id)
  const count = 3 + (h % 4) // 3-6 tags
  const picked: string[] = []
  for (let i = 0; i < count; i++) {
    picked.push(TAG_POOL[(h + i * 37) % TAG_POOL.length])
  }
  return [...new Set(picked)]
}

function defaultResolutionFor(status: Video["status"], archivedDaysAgo?: number): string | null {
  if (status === "archived" || archivedDaysAgo != null) return "720p"
  return null
}

function defaultBitrateFor(status: Video["status"], archivedDaysAgo?: number): number | null {
  if (status === "archived" || archivedDaysAgo != null) return 2500
  return null
}

function makeVideo(args: VideoInput): Video {
  const archived = args.status === "archived" || args.archivedDaysAgo != null
  const localPath =
    args.localPath ??
    (archived ? `~/Archive/${args.channelId}/${args.id}.mp4` : null)
  const h = seededHash(args.id)
  const fileSizeBytes =
    args.fileSizeBytes ?? (archived ? 120_000_000 + (h % 600_000_000) : null)
  const viewCount = args.viewCount ?? 50_000 + (h % 9_950_000)
  const commentCount = args.commentCount ?? 200 + (h % 18_000)
  return {
    id: args.id,
    channelId: args.channelId,
    title: args.title,
    description: args.description,
    uploadDate: daysAgo(args.daysSinceUpload),
    durationSec: args.durationSec,
    thumbnailUrl: `https://picsum.photos/seed/aether-v-${args.id}/640/360`,
    status: args.status,
    privacy: args.privacy ?? "public",
    type: args.type ?? "video",
    viewCount,
    tags: args.tags ?? defaultTagsFor(args.id),
    commentCount,
    comments: args.comments ?? [],
    captionLanguages: args.captionLanguages ?? (archived ? ["en"] : []),
    videoFormat: archived ? "mp4" : null,
    videoResolution: defaultResolutionFor(args.status, args.archivedDaysAgo),
    videoBitrateKbps: defaultBitrateFor(args.status, args.archivedDaysAgo),
    videoCodec: archived ? "h264" : null,
    videoFps: archived ? 30 : null,
    audioCodec: archived ? "aac" : null,
    audioBitrateKbps: archived ? 192 : null,
    fileSha256: null,
    localPath,
    fileSizeBytes,
    firstSeenAt: daysAgo(args.daysSinceUpload),
    archivedAt: args.archivedDaysAgo != null ? daysAgo(args.archivedDaysAgo) : null,
    archivedMaxResolution: archived ? "720p" : null,
    archivedCodecPreference: archived ? "compat" : null,
    lastYoutubeCheckAt: daysAgo(1),
    deletedOnYoutubeAt: args.deletedDaysAgo != null ? daysAgo(args.deletedDaysAgo) : null,
    syncProgress: args.syncProgress,
  }
}

const SAMPLE_COMMENTS: VideoComment[] = [
  {
    id: "c1",
    author: "PendulumPhysicist",
    authorAvatarUrl: "https://picsum.photos/seed/aether-u-1/48/48",
    text: "Watched this three times. The counterintuitive bit about energy transfer at 7:42 still breaks my brain — any good follow-up papers?",
    likes: 4_200,
    publishedAt: daysAgo(13),
    replyCount: 18,
  },
  {
    id: "c2",
    author: "mercato.quiet",
    authorAvatarUrl: "https://picsum.photos/seed/aether-u-2/48/48",
    text: "This is the kind of video that used to land me in the school library until closing. Thanks for making it free for the rest of us.",
    likes: 2_815,
    publishedAt: daysAgo(12),
    replyCount: 3,
  },
  {
    id: "c3",
    author: "Kaori_Inagawa",
    authorAvatarUrl: "https://picsum.photos/seed/aether-u-3/48/48",
    text: "Small correction: at 3:15 the equation should use the reduced mass, not the total mass. Doesn't change the conclusion but bugged me.",
    likes: 1_430,
    publishedAt: daysAgo(11),
    replyCount: 27,
  },
  {
    id: "c4",
    author: "Thrylos",
    authorAvatarUrl: "https://picsum.photos/seed/aether-u-4/48/48",
    text: "I'm archiving this one. Pinned in my personal vault forever.",
    likes: 902,
    publishedAt: daysAgo(10),
    replyCount: 1,
  },
  {
    id: "c5",
    author: "quiet_linnet",
    authorAvatarUrl: "https://picsum.photos/seed/aether-u-5/48/48",
    text: "At 9:58 the visualization deserves its own standalone video. Masterful work.",
    likes: 611,
    publishedAt: daysAgo(9),
    replyCount: 0,
  },
]

export const mockVideos: Video[] = [
  // Veritasium — mature archive, one deleted with local copy, one downloading
  makeVideo({ id: "ver-1", channelId: "UCHnyfMqiRRG1u-2MsSQLbXA", title: "The Biggest Misconception in Physics", description: "The most counterintuitive result in all of physics, and why it matters. A long-form essay tracing how a single graduate-student mistake propagated through textbooks for decades — and what you really should have been taught about conservation laws.", daysSinceUpload: 14, durationSec: 1275, status: "archived", archivedDaysAgo: 13, tags: ["physics", "science", "education", "longform", "conservation", "thermodynamics"], comments: SAMPLE_COMMENTS, commentCount: 18_420 }),
  makeVideo({ id: "ver-2", channelId: "UCHnyfMqiRRG1u-2MsSQLbXA", title: "What Does a 4D Ball Look Like in Real Life?", description: "A visual exploration of higher-dimensional geometry.", daysSinceUpload: 28, durationSec: 945, status: "archived", archivedDaysAgo: 27 }),
  makeVideo({ id: "ver-3", channelId: "UCHnyfMqiRRG1u-2MsSQLbXA", title: "The Infinite Pattern That Never Repeats", description: "Penrose tilings and the mathematics of aperiodic order.", daysSinceUpload: 52, durationSec: 1512, status: "archived", archivedDaysAgo: 50 }),
  makeVideo({ id: "ver-4", channelId: "UCHnyfMqiRRG1u-2MsSQLbXA", title: "Why Machines That Bend Are Better", description: "Compliant mechanisms and the future of robotics.", daysSinceUpload: 89, durationSec: 1128, status: "archived", archivedDaysAgo: 86 }),
  makeVideo({ id: "ver-5", channelId: "UCHnyfMqiRRG1u-2MsSQLbXA", title: "An Interview That Got Pulled From YouTube", description: "A contested long-form interview, now only available in personal archives.", daysSinceUpload: 210, durationSec: 1832, status: "deleted_on_youtube", archivedDaysAgo: 208, deletedDaysAgo: 4, privacy: "unlisted" }),
  makeVideo({ id: "ver-6", channelId: "UCHnyfMqiRRG1u-2MsSQLbXA", title: "Parallel Worlds Probably Exist. Here's Why.", description: "Many-worlds interpretation explained.", daysSinceUpload: 142, durationSec: 1685, status: "archived", archivedDaysAgo: 140 }),
  makeVideo({ id: "ver-7", channelId: "UCHnyfMqiRRG1u-2MsSQLbXA", title: "The Longest Running Evolution Experiment", description: "Lenski's E. coli experiment after 33+ years.", daysSinceUpload: 175, durationSec: 1425, status: "discovered" }),
  makeVideo({ id: "ver-8", channelId: "UCHnyfMqiRRG1u-2MsSQLbXA", title: "How To Slow Aging (And Even Reverse It)", description: "A deep dive on longevity science.", daysSinceUpload: 7, durationSec: 2104, status: "syncing", syncProgress: 0.42 }),
  makeVideo({ id: "ver-9", channelId: "UCHnyfMqiRRG1u-2MsSQLbXA", title: "A 15-second physics trick", description: "Short-form quick take on a surprising physical intuition.", daysSinceUpload: 1, durationSec: 55, status: "discovered", type: "short" }),
  makeVideo({ id: "ver-10", channelId: "UCHnyfMqiRRG1u-2MsSQLbXA", title: "The Math Problem That Shocked Me", description: "A surprising result in elementary arithmetic.", daysSinceUpload: 320, durationSec: 720, status: "archived", archivedDaysAgo: 315 }),

  // Tom Scott — mix, including deleted + failed
  makeVideo({ id: "tom-1", channelId: "UCBa659QWEk1AI4Tg--mrJ2A", title: "The joke that's lasted 1,100 years", description: "A strange linguistic artifact from the Old English period.", daysSinceUpload: 10, durationSec: 310, status: "archived", archivedDaysAgo: 10 }),
  makeVideo({ id: "tom-2", channelId: "UCBa659QWEk1AI4Tg--mrJ2A", title: "The most complicated address in the world", description: "Navigating a mail route that shouldn't exist.", daysSinceUpload: 45, durationSec: 415, status: "archived", archivedDaysAgo: 44 }),
  makeVideo({ id: "tom-3", channelId: "UCBa659QWEk1AI4Tg--mrJ2A", title: "The video YouTube didn't want me to make", description: "Pulled for reasons never explained.", daysSinceUpload: 520, durationSec: 298, status: "deleted_on_youtube", archivedDaysAgo: 515, deletedDaysAgo: 30 }),
  makeVideo({ id: "tom-4", channelId: "UCBa659QWEk1AI4Tg--mrJ2A", title: "Why it's almost impossible to jump higher", description: "Human jumping records and what's physically possible.", daysSinceUpload: 78, durationSec: 362, status: "archived", archivedDaysAgo: 77 }),
  makeVideo({ id: "tom-5", channelId: "UCBa659QWEk1AI4Tg--mrJ2A", title: "The lost Roman tomb beneath a British high street", description: "Archaeology where you'd least expect it.", daysSinceUpload: 23, durationSec: 425, status: "discovered" }),
  makeVideo({ id: "tom-6", channelId: "UCBa659QWEk1AI4Tg--mrJ2A", title: "The hotel with a fake front door", description: "A quirky architectural trick.", daysSinceUpload: 130, durationSec: 280, status: "archived", archivedDaysAgo: 128 }),
  makeVideo({ id: "tom-7", channelId: "UCBa659QWEk1AI4Tg--mrJ2A", title: "The broadcast signal that just won't die", description: "Legacy radio and its ghosts.", daysSinceUpload: 180, durationSec: 392, status: "failed", privacy: "members" }),
  makeVideo({ id: "tom-8", channelId: "UCBa659QWEk1AI4Tg--mrJ2A", title: "Live Q&A from the Arctic circle", description: "Two-hour live session with researchers. Archived stream.", daysSinceUpload: 260, durationSec: 7420, status: "archived", archivedDaysAgo: 258, type: "livestream" }),

  // Kurzgesagt — fresh, all discovered
  makeVideo({ id: "krz-1", channelId: "UCsXVk37bltHxD1rDPwtNM8Q", title: "What if We Terraformed Venus?", description: "Turning Earth's hellish twin into a second home.", daysSinceUpload: 12, durationSec: 640, status: "discovered" }),
  makeVideo({ id: "krz-2", channelId: "UCsXVk37bltHxD1rDPwtNM8Q", title: "The Most Dangerous Stuff in the Universe", description: "Strange matter and quark stars.", daysSinceUpload: 40, durationSec: 580, status: "discovered" }),
  makeVideo({ id: "krz-3", channelId: "UCsXVk37bltHxD1rDPwtNM8Q", title: "How to Move the Sun", description: "The engineering of stellar propulsion.", daysSinceUpload: 74, durationSec: 625, status: "discovered" }),
  makeVideo({ id: "krz-4", channelId: "UCsXVk37bltHxD1rDPwtNM8Q", title: "We Were Wrong About Aging", description: "New research that upends what we know.", daysSinceUpload: 110, durationSec: 558, status: "discovered" }),
  makeVideo({ id: "krz-5", channelId: "UCsXVk37bltHxD1rDPwtNM8Q", title: "What If the Moon Was a Disco Ball?", description: "A whimsical thought experiment with serious physics.", daysSinceUpload: 155, durationSec: 495, status: "discovered" }),

  // Numberphile — currently downloading, plus a deleted without local copy (unrecoverable)
  makeVideo({ id: "num-1", channelId: "UCoxcjq-8xIDTYp3uz647V5A", title: "The Goat Problem", description: "A geometry puzzle with a shockingly hard exact answer.", daysSinceUpload: 8, durationSec: 854, status: "syncing", syncProgress: 0.18 }),
  makeVideo({ id: "num-2", channelId: "UCoxcjq-8xIDTYp3uz647V5A", title: "Why π is still fascinating", description: "Fresh results in an ancient constant.", daysSinceUpload: 22, durationSec: 712, status: "syncing", syncProgress: 0.67 }),
  makeVideo({ id: "num-3", channelId: "UCoxcjq-8xIDTYp3uz647V5A", title: "The largest prime number... again", description: "Mersenne primes and the hunt for bigger.", daysSinceUpload: 65, durationSec: 635, status: "archived", archivedDaysAgo: 62 }),
  makeVideo({ id: "num-4", channelId: "UCoxcjq-8xIDTYp3uz647V5A", title: "A strange pattern in the primes", description: "Surprising statistical regularities.", daysSinceUpload: 92, durationSec: 910, status: "archived", archivedDaysAgo: 89 }),
  makeVideo({ id: "num-5", channelId: "UCoxcjq-8xIDTYp3uz647V5A", title: "Graham's Number, revisited", description: "A gentler introduction to the famously huge.", daysSinceUpload: 143, durationSec: 1124, status: "discovered", privacy: "unlisted" }),
  makeVideo({ id: "num-6", channelId: "UCoxcjq-8xIDTYp3uz647V5A", title: "The hardest number puzzle ever solved", description: "A decades-long saga with a recent resolution.", daysSinceUpload: 210, durationSec: 728, status: "deleted_on_youtube", deletedDaysAgo: 12, localPath: null, fileSizeBytes: null }),
]

export function getChannelById(id: string) {
  return mockChannels.find((c) => c.id === id)
}

export function getVideosForChannel(channelId: string) {
  return mockVideos.filter((v) => v.channelId === channelId)
}

export function getArchivedCount(channelId: string) {
  return mockVideos.filter(
    (v) =>
      v.channelId === channelId &&
      (v.status === "archived" || (v.status === "deleted_on_youtube" && v.localPath !== null))
  ).length
}
