import * as React from "react"
import { useNavigate } from "react-router-dom"
import {
  ArrowDown,
  ArrowUp,
  LayoutGrid,
  LayoutList,
  Search,
  Settings,
  SlidersHorizontal,
  Tv,
  Video as VideoIcon,
} from "lucide-react"
import { AddChannelForm, type ParsedChannelUrl } from "@/components/AddChannelForm"
import { ChannelCard } from "@/components/ChannelCard"
import { ChannelListRow } from "@/components/ChannelListRow"
import { VideoCard } from "@/components/VideoCard"
import { Button } from "@/components/ui/button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import { cn } from "@/lib/utils"
import { YouTubeSettingsPanel } from "@/components/YouTubeSettingsPanel"
import { useToast } from "@/components/ui/toast"
import { usePanelParam } from "@/lib/usePanelParam"
import { formatTimeUntil } from "@/lib/format"
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { useDocumentTitle } from "@/lib/useDocumentTitle"
import {
  defaultChannelSettings,
  mockChannels,
  normalizeChannelSettings,
} from "@/lib/mockData"
import type {
  Channel,
  ChannelArchiveSettings,
  SortDimension,
  Video,
  VideoType,
  VideoVisibility,
} from "@/lib/types"
import { Paywalled } from "@/components/Paywalled"
import {
  BILLING_CHANGE_EVENT,
  readEffectivePaymentStatus,
  refreshBillingStatus,
  isPaidWorkLocked,
} from "@/lib/paymentStatus"

// What a list of channels can be ordered by.
//
// Deliberately smaller than the videos page's vocabulary. Channels are a
// handful, not thousands, so saved presets and a filter popover would be
// machinery without a job here - these six answer every question anyone
// actually has about a channel list.
type ChannelSortDimension =
  | "added"
  | "name"
  | "storage"
  | "videos"
  | "cost"
  | "synced"

const CHANNEL_SORT_LABELS: Record<ChannelSortDimension, string> = {
  added: "Date added",
  name: "Name",
  storage: "Storage",
  videos: "Videos archived",
  cost: "Cost",
  synced: "Last synced",
}

// Everything a channel list can be narrowed by.
//
// A popover rather than a row of chips: chips only fit a single
// question, and the real ones are several - is it running, does the
// worker have access, is it still on YouTube, when did I add it. Same
// shape as the videos page's filter so the two read as one product.
type ChannelStatus = "active" | "paused"
type ChannelAuth = "authenticated" | "unauthenticated"
type ChannelHealth = "available" | "terminated"

const STATUS_OPTIONS: { value: ChannelStatus; label: string }[] = [
  { value: "active", label: "Active" },
  { value: "paused", label: "Paused" },
]
const AUTH_OPTIONS: { value: ChannelAuth; label: string }[] = [
  { value: "authenticated", label: "Authenticated" },
  { value: "unauthenticated", label: "Not authenticated" },
]
const HEALTH_OPTIONS: { value: ChannelHealth; label: string }[] = [
  { value: "available", label: "On YouTube" },
  { value: "terminated", label: "Terminated" },
]

/** Whether the worker has proven access to this channel's private
 *  videos. Revoking withdraws it, so a revoked channel reads as
 *  unauthenticated rather than as its own third state. */
function isAuthenticated(c: Channel): boolean {
  return c.ownershipRevoked !== true && (c.authenticated ?? false)
}

// How this user likes their channel list, stored on the account rather
// than in this browser.
//
// The old view-mode preference lived in localStorage, which meant it
// followed the machine instead of the person: sign in on a laptop and
// you got someone else's default back. These ride in the YouTube
// settings blob, which the backend stores verbatim and which
// _NEW_CHANNEL_DEFAULT_KEYS deliberately does not copy into new
// channels - so a display preference can never leak into a channel's
// archiving behaviour.
//
// Search is NOT persisted. A filter is a way you like to work; a search
// is a question you asked once, and restoring it a week later would
// hide channels for a reason nobody remembers typing.
type ChannelListPrefs = {
  view: "grid" | "list"
  sortDimension: ChannelSortDimension
  sortDirection: "asc" | "desc"
  status: ChannelStatus[]
  auth: ChannelAuth[]
  health: ChannelHealth[]
  addedFrom: string
  addedTo: string
}

/** What the page is listing. Not a filter - it decides which entity
 *  the toolbar's filters and sorts even apply to, which is why the
 *  control for it sits outside the filter popover rather than in it. */
type ListScope = "channels" | "videos"

/** Backup state, as opposed to VideoVisibility which is YouTube's
 *  privacy. "Have you got a copy of this?" and "who can watch it?" are
 *  different questions and users ask both. */
type VideoSyncState = "archived" | "failed" | "pending"

type VideoListPrefs = {
  view: "grid" | "list"
  sortDimension: SortDimension
  sortDirection: "asc" | "desc"
  visibilities: VideoVisibility[]
  types: VideoType[]
  sync: VideoSyncState[]
  uploadedFrom: string
  uploadedTo: string
}

const DEFAULT_VIDEO_PREFS: VideoListPrefs = {
  view: "grid",
  sortDimension: "upload",
  sortDirection: "desc",
  visibilities: [],
  types: [],
  sync: [],
  uploadedFrom: "",
  uploadedTo: "",
}

const VIDEO_SORT_LABELS: Record<SortDimension, string> = {
  upload: "Upload date",
  views: "Views",
  filesize: "File size",
  duration: "Duration",
}

const VIDEO_VISIBILITY_OPTIONS: { value: VideoVisibility; label: string }[] = [
  { value: "public", label: "Public" },
  { value: "unlisted", label: "Unlisted" },
  { value: "private", label: "Private" },
  { value: "members", label: "Members" },
  // Stored as "deleted", shown as "Unavailable": all our detection
  // proves is that we could not see it on YouTube when we last looked.
  { value: "deleted", label: "Unavailable" },
]

const VIDEO_SYNC_OPTIONS: { value: VideoSyncState; label: string }[] = [
  { value: "archived", label: "Backed up" },
  { value: "failed", label: "Failed" },
  { value: "pending", label: "Not backed up" },
]

/** Video.status carries both backup state and one visibility fact
 *  ("deleted_on_youtube"), so map rather than compare directly. */
function videoSyncOf(v: Video): VideoSyncState | null {
  switch (v.status) {
    case "archived":
      return "archived"
    case "failed":
      return "failed"
    // In progress is not a state the user needs to act on, and it is
    // gone within minutes. What they are actually asking when they
    // filter is "what do you not have yet", which is the same answer
    // either way.
    case "syncing":
    case "discovered":
      return "pending"
    default:
      return null
  }
}

const VIDEO_TYPE_OPTIONS: { value: VideoType; label: string }[] = [
  { value: "video", label: "Video" },
  { value: "short", label: "Short" },
  { value: "livestream", label: "Livestream" },
]

/** The filter's notion of visibility. Video.visibility is our own
 *  frozen open/sealed tier; what a user is looking for here is
 *  YouTube's privacy, with "unavailable" folded in as its own bucket. */
function videoVisibilityOf(v: Video): VideoVisibility {
  return v.status === "deleted_on_youtube" ? "deleted" : v.privacy
}

const DEFAULT_LIST_PREFS: ChannelListPrefs = {
  view: "grid",
  sortDimension: "added",
  sortDirection: "desc",
  status: [],
  auth: [],
  health: [],
  addedFrom: "",
  addedTo: "",
}

/** The Filter popover's contents when the page is listing videos.
 *  Channels and videos share the popover but nothing inside it: a
 *  channel is active or paused, a video is public or private. That is
 *  the whole reason the scope switch cannot live in here - it would be
 *  a control that replaces the menu it sits in. */
function VideoFilterPanel({
  prefs,
  onChange,
  activeCount,
}: {
  prefs: VideoListPrefs
  onChange: (next: Partial<VideoListPrefs>) => void
  activeCount: number
}) {
  const toggle = <T extends string>(list: T[], value: T): T[] =>
    list.includes(value) ? list.filter((v) => v !== value) : [...list, value]

  return (
    <div className="space-y-5">
      <FilterChips
        label="Visibility"
        options={VIDEO_VISIBILITY_OPTIONS}
        selected={new Set(prefs.visibilities)}
        onToggle={(v) =>
          onChange({ visibilities: toggle(prefs.visibilities, v) })
        }
      />
      <FilterChips
        label="Backup"
        options={VIDEO_SYNC_OPTIONS}
        selected={new Set(prefs.sync)}
        onToggle={(v) => onChange({ sync: toggle(prefs.sync, v) })}
      />
      <FilterChips
        label="Type"
        options={VIDEO_TYPE_OPTIONS}
        selected={new Set(prefs.types)}
        onToggle={(v) => onChange({ types: toggle(prefs.types, v) })}
      />
      <div>
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-2">
          Uploaded
        </div>
        <div className="grid grid-cols-2 gap-2">
          <div>
            <label className="text-[10px] text-muted-foreground block mb-1">
              From
            </label>
            <input
              type="date"
              value={prefs.uploadedFrom}
              onChange={(e) => onChange({ uploadedFrom: e.target.value })}
              className="h-9 w-full border border-border bg-transparent px-2 text-xs text-foreground outline-none focus:border-white"
            />
          </div>
          <div>
            <label className="text-[10px] text-muted-foreground block mb-1">
              To
            </label>
            <input
              type="date"
              value={prefs.uploadedTo}
              onChange={(e) => onChange({ uploadedTo: e.target.value })}
              className="h-9 w-full border border-border bg-transparent px-2 text-xs text-foreground outline-none focus:border-white"
            />
          </div>
        </div>
      </div>
      {activeCount > 0 && (
        <div className="flex justify-end pt-1">
          <button
            type="button"
            onClick={() =>
              onChange({
                visibilities: [],
                types: [],
                sync: [],
                uploadedFrom: "",
                uploadedTo: "",
              })
            }
            className="text-xs text-muted-foreground cursor-pointer font-semibold"
          >
            Reset filters
          </button>
        </div>
      )}
    </div>
  )
}


function FilterChips<T extends string>({
  label,
  options,
  selected,
  onToggle,
}: {
  label: string
  options: { value: T; label: string }[]
  selected: Set<T>
  onToggle: (value: T) => void
}) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-2">
        {label}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {options.map((o) => (
          <button
            key={o.value}
            type="button"
            onClick={() => onToggle(o.value)}
            className={cn(
              "px-3 py-1 text-xs font-semibold border cursor-pointer",
              selected.has(o.value)
                ? "bg-white text-black border-white"
                : "border-border text-foreground"
            )}
          >
            {o.label}
          </button>
        ))}
      </div>
    </div>
  )
}

function compareChannels(
  a: Channel,
  b: Channel,
  dimension: ChannelSortDimension
): number {
  switch (dimension) {
    case "name":
      // localeCompare so accents and case sort the way a person reads
      // them rather than the way bytes happen to fall.
      return (a.name || a.handle).localeCompare(b.name || b.handle)
    case "storage":
      return (a.bytesStored ?? 0) - (b.bytesStored ?? 0)
    case "videos":
      return (a.archivedVideoCount ?? 0) - (b.archivedVideoCount ?? 0)
    case "cost":
      return (a.projectedMonthlyCostUsd ?? 0) - (b.projectedMonthlyCostUsd ?? 0)
    case "synced":
      // Never-synced sorts as the oldest rather than the newest, which
      // is what "least recently synced first" has to mean to be useful.
      return (
        new Date(a.lastSyncedAt || 0).getTime() -
        new Date(b.lastSyncedAt || 0).getTime()
      )
    case "added":
    default:
      return (
        new Date(a.addedAt || 0).getTime() - new Date(b.addedAt || 0).getTime()
      )
  }
}

// A channel the user owns, surfaced from their connected account (the
// worker app for Basic tier). Not yet tracked - one tap imports it.
type ConnectedChannel = {
  id: string
  youtubeId: string
  handle: string | null
  title: string | null
  thumbnailUrl: string | null
}

// A channel the user removed that's still inside the 30-day grace window.
// Kept (not billed) so it can be restored by re-adding it above, or wiped
// right now via the "Delete permanently" action.
type RemovedChannel = {
  id: string
  name: string
  handle: string
  avatarUrl: string
  removedAt: string | null
  purgeAt: string | null
}

export default function YouTube() {
  const [channels, setChannels] = React.useState<Channel[]>(mockChannels)
  const [viewMode, setViewMode] = React.useState<"grid" | "list">(
    DEFAULT_LIST_PREFS.view
  )
  const [sortDimension, setSortDimension] = React.useState<ChannelSortDimension>(
    DEFAULT_LIST_PREFS.sortDimension
  )
  const [sortDirection, setSortDirection] = React.useState<"asc" | "desc">(
    DEFAULT_LIST_PREFS.sortDirection
  )
  const [statusFilter, setStatusFilter] = React.useState<Set<ChannelStatus>>(
    new Set()
  )
  const [authFilter, setAuthFilter] = React.useState<Set<ChannelAuth>>(new Set())
  const [healthFilter, setHealthFilter] = React.useState<Set<ChannelHealth>>(
    new Set()
  )
  const [addedFrom, setAddedFrom] = React.useState("")
  const [addedTo, setAddedTo] = React.useState("")
  const [search, setSearch] = React.useState("")

  // ---- Videos scope -------------------------------------------------
  // The page lists channels or every video across them. Scope is not a
  // filter: it decides which set of filters and sorts the toolbar shows
  // at all, so it lives to the left of everything it governs.
  const [scope, setScope] = React.useState<ListScope>("channels")
  const [videos, setVideos] = React.useState<Video[]>([])
  const [videosLoading, setVideosLoading] = React.useState(false)
  const videosLoadedRef = React.useRef(false)
  const [videoPrefs, setVideoPrefs] =
    React.useState<VideoListPrefs>(DEFAULT_VIDEO_PREFS)

  // Set once the server's prefs have been applied. Without it the save
  // effect below fires on first render and writes the DEFAULTS over
  // whatever the user had, which is the exact opposite of persisting.
  const prefsLoadedRef = React.useRef(false)
  // The ref cannot wake an effect. This mirrors it for the one effect
  // that has to run *after* the saved prefs arrive.
  const [prefsLoaded, setPrefsLoaded] = React.useState(false)

  // Empty set = no opinion, so every option unticked shows everything
  // rather than nothing. Same rule as the videos page: a filter nobody
  // has touched must not hide anything.
  const activeFilterCount =
    statusFilter.size +
    authFilter.size +
    healthFilter.size +
    (addedFrom ? 1 : 0) +
    (addedTo ? 1 : 0)

  const resetFilters = () => {
    setStatusFilter(new Set())
    setAuthFilter(new Set())
    setHealthFilter(new Set())
    setAddedFrom("")
    setAddedTo("")
  }

  function toggleIn<T>(
    set: Set<T>,
    value: T,
    apply: (next: Set<T>) => void
  ): void {
    const next = new Set(set)
    if (next.has(value)) next.delete(value)
    else next.add(value)
    apply(next)
  }
  const [globalSettings, setGlobalSettings] =
    React.useState<ChannelArchiveSettings>(defaultChannelSettings)
  // URL-backed so refresh keeps the settings sheet open. See
  // usePanelParam for the param-key convention shared across pages.
  const [settingsOpen, setSettingsOpen] = usePanelParam("settings")
  const { toast } = useToast()
  const navigate = useNavigate()
  const [connectedChannels, setConnectedChannels] = React.useState<
    ConnectedChannel[]
  >([])
  const [importing, setImporting] = React.useState<string | null>(null)
  const [removedChannels, setRemovedChannels] = React.useState<RemovedChannel[]>(
    []
  )
  // Payment gate for the "Your channels" (connected-account) section: re-grabbing
  // your channels needs an active plan (matches the import endpoint's 402).
  const [paymentStatus, setPaymentStatus] = React.useState<string | null>(() =>
    readEffectivePaymentStatus()
  )
  React.useEffect(() => {
    const sync = () => setPaymentStatus(readEffectivePaymentStatus())
    window.addEventListener(BILLING_CHANGE_EVENT, sync)
    window.addEventListener("storage", sync)
    void refreshBillingStatus()
    return () => {
      window.removeEventListener(BILLING_CHANGE_EVENT, sync)
      window.removeEventListener("storage", sync)
    }
  }, [])

  // Load saved YouTube settings + channel list on mount.
  React.useEffect(() => {
    let cancelled = false

    fetch("/api/youtube/settings", { credentials: "include" })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (!cancelled && data) {
          setGlobalSettings(
            normalizeChannelSettings(data as Partial<ChannelArchiveSettings>)
          )
          // Toolbar preferences ride in the same blob. Absent for a new
          // account, which is exactly how a new account gets the
          // defaults without anything special-casing "new".
          const saved = (data as { channelList?: Partial<ChannelListPrefs> })
            .channelList
          if (saved) {
            if (saved.view === "list" || saved.view === "grid")
              setViewMode(saved.view)
            if (saved.sortDimension) setSortDimension(saved.sortDimension)
            if (saved.sortDirection === "asc" || saved.sortDirection === "desc")
              setSortDirection(saved.sortDirection)
            if (saved.status) setStatusFilter(new Set(saved.status))
            if (saved.auth) setAuthFilter(new Set(saved.auth))
            if (saved.health) setHealthFilter(new Set(saved.health))
            setAddedFrom(saved.addedFrom ?? "")
            setAddedTo(saved.addedTo ?? "")
          }
          const blob = data as {
            listScope?: ListScope
            videoList?: Partial<VideoListPrefs>
          }
          if (blob.listScope === "videos" || blob.listScope === "channels")
            setScope(blob.listScope)
          if (blob.videoList) {
            const saved = blob.videoList
            setVideoPrefs((prev) => ({
              ...prev,
              ...saved,
              // Drop values this build no longer knows. A stale
              // "syncing" would match nothing and read as an empty
              // library rather than as a filter that needs clearing.
              sync: (saved.sync ?? prev.sync).filter(
                (v): v is VideoSyncState =>
                  VIDEO_SYNC_OPTIONS.some((o) => o.value === v)
              ),
            }))
          }
          prefsLoadedRef.current = true
          setPrefsLoaded(true)
        }
      })
      .catch(() => {
        // Silent - user keeps frontend defaults if backend is
        // unreachable. prefsLoadedRef stays false so we never write
        // defaults over prefs we failed to read, but the deep link
        // still needs to fire: arriving from "Review" and seeing an
        // unfiltered list is worse than arriving with stale defaults.
        setPrefsLoaded(true)
      })

    fetch("/api/youtube/channels", { credentials: "include" })
      .then((res) => (res.ok ? res.json() : []))
      .then((data) => {
        if (!cancelled && Array.isArray(data)) {
          const channelsArr = data as Channel[]
          setChannels(
            channelsArr.map((c) => ({
              ...c,
              settings: normalizeChannelSettings(c.settings),
            }))
          )
        }
      })
      .catch(() => {
        // Silent — empty dashboard if backend is unreachable.
      })

    fetch("/api/youtube/connected-channels", { credentials: "include" })
      .then((res) => (res.ok ? res.json() : []))
      .then((data) => {
        if (!cancelled && Array.isArray(data)) {
          setConnectedChannels(data as ConnectedChannel[])
        }
      })
      .catch(() => {
        // Silent - no import suggestions if backend is unreachable.
      })

    fetch("/api/youtube/channels/removed", { credentials: "include" })
      .then((res) => (res.ok ? res.json() : []))
      .then((data) => {
        if (!cancelled && Array.isArray(data)) {
          setRemovedChannels(data as RemovedChannel[])
        }
      })
      .catch(() => {
        // Silent - no "recently removed" section if backend is unreachable.
      })

    return () => {
      cancelled = true
    }
  }, [])

  // Sorted copy, never a sort in place: `channels` is also what the
  // optimistic active-toggle writes back into, and reordering it under
  // an in-flight request is how a revert lands on the wrong row.
  const sortedChannels = React.useMemo(() => {
    const q = search.trim().toLowerCase()
    const copy = channels.filter((c) => {
      if (statusFilter.size) {
        const v: ChannelStatus = c.settings.active ? "active" : "paused"
        if (!statusFilter.has(v)) return false
      }
      if (authFilter.size) {
        const v: ChannelAuth = isAuthenticated(c)
          ? "authenticated"
          : "unauthenticated"
        if (!authFilter.has(v)) return false
      }
      if (healthFilter.size) {
        const v: ChannelHealth =
          c.youtubeStatus === "terminated" ? "terminated" : "available"
        if (!healthFilter.has(v)) return false
      }
      // Date-only comparison on the added timestamp. The `to` bound is
      // inclusive of its whole day, which is what someone picking a date
      // means by it.
      if (addedFrom && (c.addedAt || "").slice(0, 10) < addedFrom) return false
      if (addedTo && (c.addedAt || "").slice(0, 10) > addedTo) return false
      if (q && !`${c.name} ${c.handle}`.toLowerCase().includes(q)) return false
      return true
    })
    copy.sort((a, b) => {
      const cmp = compareChannels(a, b, sortDimension)
      return sortDirection === "asc" ? cmp : -cmp
    })
    return copy
  }, [
    channels,
    sortDimension,
    sortDirection,
    statusFilter,
    authFilter,
    healthFilter,
    addedFrom,
    addedTo,
    search,
  ])

  // A deep link wins over saved preferences. The home page's "Review"
  // button points here with ?scope=videos&sync=failed, and landing on
  // the user's last-used filter instead of the one the link asked for
  // would show them a different set of videos than the banner counted.
  //
  // Applied after the saved prefs load rather than alongside, because
  // that fetch resolves later and would otherwise overwrite this.
  const deepLinkRef = React.useRef(false)
  React.useEffect(() => {
    if (deepLinkRef.current || !prefsLoaded) return
    deepLinkRef.current = true

    const params = new URLSearchParams(window.location.search)
    const wanted = params.get("scope")
    const sync = params.get("sync")
    if (wanted !== "videos" && !sync) return

    setScope("videos")
    if (sync) {
      const states = sync
        .split(",")
        .filter((v): v is VideoSyncState =>
          VIDEO_SYNC_OPTIONS.some((o) => o.value === v)
        )
      if (states.length) setVideoPrefs((p) => ({ ...p, sync: states }))
    }
    // Drop the params once applied. They have done their job, and
    // leaving them means a refresh re-imposes the filter after the
    // user has cleared it.
    window.history.replaceState({}, "", window.location.pathname)
  }, [prefsLoaded])

  // Load the library the first time the user switches to Videos, then
  // keep it. Every page is walked back-to-back rather than lazily,
  // because sorting by size or duration is only correct over the whole
  // set - a partial load would silently sort the first page.
  React.useEffect(() => {
    if (scope !== "videos" || videosLoadedRef.current) return
    videosLoadedRef.current = true
    setVideosLoading(true)
    let cancelled = false

    const run = async () => {
      const all: Video[] = []
      let cursor: string | null = null
      for (let page = 0; page < 50; page += 1) {
        const url = new URL("/api/youtube/videos", window.location.origin)
        url.searchParams.set("limit", "200")
        if (cursor) url.searchParams.set("cursor", cursor)
        const res = await fetch(url.toString(), { credentials: "include" })
        if (!res.ok) break
        const body = (await res.json()) as {
          items: Video[]
          nextCursor: string | null
        }
        all.push(...body.items)
        if (!body.nextCursor) break
        cursor = body.nextCursor
      }
      if (cancelled) return
      setVideos(all)
      setVideosLoading(false)

      // Thumbnails are presigned separately, the same way the channel
      // page does it: the listing endpoint deliberately returns the
      // stored URL rather than minting one per row. Grouped by channel
      // because the presign endpoint is per channel, and chunked
      // because it caps a single call at 500 ids.
      const byChannel = new Map<string, string[]>()
      for (const v of all) {
        if (!v.channelId) continue
        const list = byChannel.get(v.channelId)
        if (list) list.push(v.id)
        else byChannel.set(v.channelId, [v.id])
      }

      const urls: Record<string, string> = {}
      for (const [channelId, ids] of byChannel) {
        for (let i = 0; i < ids.length; i += 500) {
          try {
            const res = await fetch(
              `/api/youtube/channels/${encodeURIComponent(channelId)}/thumbnail-urls`,
              {
                method: "POST",
                credentials: "include",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ video_ids: ids.slice(i, i + 500) }),
              }
            )
            if (!res.ok) continue
            const body = (await res.json()) as {
              urls?: Record<string, string>
            }
            Object.assign(urls, body.urls ?? {})
          } catch {
            // A channel whose thumbnails fail to presign just renders
            // the placeholder layout, which is what a video with no
            // archived thumbnail shows anyway.
          }
        }
      }

      if (cancelled || Object.keys(urls).length === 0) return
      setVideos((prev) =>
        prev.map((v) =>
          urls[v.id] ? { ...v, thumbnailUrl: urls[v.id] } : v
        )
      )
    }

    void run().catch(() => {
      if (!cancelled) {
        setVideosLoading(false)
        // Let the user try again rather than stranding them on an
        // empty library that looks like "you have no videos".
        videosLoadedRef.current = false
      }
    })

    return () => {
      cancelled = true
    }
  }, [scope])

  const visibleVideos = React.useMemo(() => {
    const q = search.trim().toLowerCase()
    const filtered = videos.filter((v) => {
      if (q && !v.title.toLowerCase().includes(q)) {
        // Channel name is searchable too - in a mixed list "show me
        // everything from AFRFX" is the obvious thing to type.
        if (!(v.channelName ?? "").toLowerCase().includes(q)) return false
      }
      if (
        videoPrefs.visibilities.length &&
        !videoPrefs.visibilities.includes(videoVisibilityOf(v))
      )
        return false
      if (videoPrefs.types.length && !videoPrefs.types.includes(v.type))
        return false
      if (videoPrefs.sync.length) {
        const state = videoSyncOf(v)
        if (!state || !videoPrefs.sync.includes(state)) return false
      }
      const day = (v.uploadDate || "").slice(0, 10)
      if (videoPrefs.uploadedFrom && day && day < videoPrefs.uploadedFrom)
        return false
      if (videoPrefs.uploadedTo && day && day > videoPrefs.uploadedTo)
        return false
      return true
    })

    const dir = videoPrefs.sortDirection === "asc" ? 1 : -1
    const key = (v: Video): number | string => {
      switch (videoPrefs.sortDimension) {
        case "views":
          return v.viewCount ?? 0
        case "filesize":
          return v.fileSizeBytes ?? 0
        case "duration":
          return v.durationSec ?? 0
        default:
          return v.uploadDate || ""
      }
    }
    return [...filtered].sort((a, b) => {
      const ka = key(a)
      const kb = key(b)
      if (ka < kb) return -1 * dir
      if (ka > kb) return 1 * dir
      return 0
    })
  }, [videos, search, videoPrefs])

  // Sort direction and grid/list are the same two controls in both
  // scopes, but each scope remembers its own answer - a list of 763
  // videos and a list of 4 channels do not want the same layout.
  const activeView = scope === "videos" ? videoPrefs.view : viewMode
  const setActiveView = (v: "grid" | "list") =>
    scope === "videos"
      ? setVideoPrefs((p) => ({ ...p, view: v }))
      : setViewMode(v)
  const activeDirection =
    scope === "videos" ? videoPrefs.sortDirection : sortDirection

  const videoFilterCount =
    videoPrefs.visibilities.length +
    videoPrefs.types.length +
    videoPrefs.sync.length +
    (videoPrefs.uploadedFrom ? 1 : 0) +
    (videoPrefs.uploadedTo ? 1 : 0)

  // Persist the toolbar whenever it changes.
  //
  // Merged into the settings blob rather than replacing it: the same
  // object holds the New-channel defaults, and a PUT that dropped them
  // would quietly reset every future channel's archiving behaviour
  // because somebody changed a sort order.
  React.useEffect(() => {
    if (!prefsLoadedRef.current) return
    const prefs: ChannelListPrefs = {
      view: viewMode,
      sortDimension,
      sortDirection,
      status: [...statusFilter],
      auth: [...authFilter],
      health: [...healthFilter],
      addedFrom,
      addedTo,
    }
    const id = window.setTimeout(() => {
      void fetch("/api/youtube/settings", {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
        ...globalSettings,
        channelList: prefs,
        videoList: videoPrefs,
        listScope: scope,
      }),
      }).catch(() => {
        // Silent. A display preference that failed to save is not worth
        // a toast over the page the user is trying to read.
      })
    }, 400)
    return () => window.clearTimeout(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    scope,
    videoPrefs,
    viewMode,
    sortDimension,
    sortDirection,
    statusFilter,
    authFilter,
    healthFilter,
    addedFrom,
    addedTo,
  ])

  const saveGlobalSettings = async (next: ChannelArchiveSettings) => {
    setGlobalSettings(next)
    try {
      const res = await fetch("/api/youtube/settings", {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...next,
          // Carry the toolbar prefs through: this PUT replaces the whole
          // blob, so omitting them would wipe the user's view and sort
          // every time they touched the New-channel defaults.
          channelList: {
            view: viewMode,
            sortDimension,
            sortDirection,
            status: [...statusFilter],
            auth: [...authFilter],
            health: [...healthFilter],
            addedFrom,
            addedTo,
          },
        }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
    } catch {
      toast({
        title: "Couldn't save YouTube settings",
        description: "Backend unreachable — changes only apply to this session.",
        variant: "error",
      })
    }
  }

  const toggleActive = async (channelId: string) => {
    const before = channels.find((c) => c.id === channelId)
    if (!before) return
    const after: Channel = {
      ...before,
      settings: { ...before.settings, active: !before.settings.active },
    }

    // Optimistic — update UI immediately, revert if the request fails.
    setChannels((curr) => curr.map((c) => (c.id === channelId ? after : c)))

    try {
      const res = await fetch(
        `/api/youtube/channels/${encodeURIComponent(channelId)}`,
        {
          method: "PUT",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(after),
        }
      )
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
    } catch {
      setChannels((curr) => curr.map((c) => (c.id === channelId ? before : c)))
      toast({
        title: "Couldn't save change",
        description: "Backend unreachable — toggle reverted.",
        variant: "error",
      })
    }
  }

  const addChannel = async (parsed: ParsedChannelUrl) => {
    // Optimistic duplicate check against what we already know about.
    // Backend re-checks against the resolved UC id (which may differ
    // from what the user typed if they pasted a handle).
    if (channels.some((c) => c.id.toLowerCase() === parsed.id.toLowerCase())) {
      toast({
        title: "Channel already added",
        description: `${parsed.handle} is already on your dashboard.`,
        variant: "error",
      })
      return
    }

    try {
      const res = await fetch("/api/youtube/channels/track", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ input: parsed.id }),
      })
      if (res.status === 409) {
        toast({
          title: "Channel already added",
          description: `${parsed.handle} is already on your account.`,
          variant: "error",
        })
        return
      }
      if (res.status === 402) {
        toast({
          title: "Add a payment method first",
          description: "Tracking a channel triggers paid backend work. Set up billing in Settings to continue.",
          variant: "error",
        })
        navigate("/settings#payment")
        return
      }
      if (res.status === 502) {
        toast({
          title: "Couldn't reach YouTube",
          description: `Couldn't fetch ${parsed.handle}. Try again in a moment.`,
          variant: "error",
        })
        return
      }
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}))
        toast({
          title: "Couldn't track channel",
          description: detail?.detail || `Server returned ${res.status}.`,
          variant: "error",
        })
        return
      }
      // Backend returns the fully-populated Channel payload; insert
      // it at the top so the new card pops in immediately.
      const saved = (await res.json()) as Channel
      setChannels((prev) => [saved, ...prev])
    } catch {
      toast({
        title: "Couldn't track channel",
        description: "Backend unreachable — channel not saved.",
        variant: "error",
      })
    }
  }

  // Import an owned channel surfaced from the user's connected account.
  // Reuses the same track endpoint as the paste-URL flow; the worker's
  // cookies unlock the owner's private + members-only videos. On success
  // the card moves out of this list and into the tracked grid below.
  const importConnected = async (cc: ConnectedChannel) => {
    setImporting(cc.id)
    try {
      const res = await fetch("/api/youtube/channels/track", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ input: cc.youtubeId }),
      })
      if (res.status === 402) {
        toast({
          title: "Add a payment method first",
          description:
            "Importing a channel triggers paid backend work. Set up billing in Settings to continue.",
          variant: "error",
        })
        navigate("/settings#payment")
        return
      }
      if (!res.ok && res.status !== 409) {
        const detail = await res.json().catch(() => ({}))
        toast({
          title: "Couldn't import channel",
          description: detail?.detail || `Server returned ${res.status}.`,
          variant: "error",
        })
        return
      }
      // Success (or 409 = already tracked): drop it from the import list.
      setConnectedChannels((prev) => prev.filter((c) => c.id !== cc.id))
      if (res.ok) {
        const saved = (await res.json()) as Channel
        setChannels((prev) => [
          saved,
          ...prev.filter((c) => c.id !== saved.id),
        ])
        const name = cc.title || cc.handle || "Your channel"
        // Whether it starts syncing depends on the New-channel-defaults
        // "Active" toggle, so tailor the message to what actually happened.
        const willSync = saved.settings?.active !== false
        toast({
          title: "Channel added",
          description: willSync
            ? `${name} will start syncing once your worker app is running.`
            : `${name} was added, paused. Flip its switch on to start syncing.`,
        })
      }
    } catch {
      toast({
        title: "Couldn't import channel",
        description: "Backend unreachable - try again.",
        variant: "error",
      })
    } finally {
      setImporting(null)
    }
  }

  // Permanently wipe a removed (graced) channel now instead of waiting out
  // the 30-day window. Irreversible — the confirmation lives in
  // RemovedChannelsSection. Throws on failure so the dialog can surface it.
  const purgeRemoved = async (id: string) => {
    const res = await fetch(
      `/api/youtube/channels/${encodeURIComponent(id)}/purge`,
      { method: "POST", credentials: "include" }
    )
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    setRemovedChannels((curr) => curr.filter((c) => c.id !== id))
  }

  useDocumentTitle("YouTube")

  return (
    <div className="p-8 max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-extrabold tracking-tight">YouTube</h1>
        <Button
          variant="outline"
          size="icon"
          aria-label="YouTube settings"
          onClick={() => setSettingsOpen(true)}
        >
          <Settings />
        </Button>
      </div>

      <div className="mb-8">
        {/* Same paywall treatment as "Your channels" below and the Worker
            App card in Settings - one lock convention, not a bespoke one
            per element. Paywalled kills pointer events, so the blurred
            input genuinely cannot be typed into. */}
        {isPaidWorkLocked(paymentStatus) ? (
          <Paywalled iconOnly>
            <AddChannelForm onAdd={addChannel} />
          </Paywalled>
        ) : (
          <AddChannelForm onAdd={addChannel} />
        )}
      </div>

      {connectedChannels.length > 0 && (
        <div className="mb-8">
          <h2 className="text-sm font-semibold mb-3">Your channels</h2>
          {/* Gated: getting your channels back requires an active plan. */}
          {!isPaidWorkLocked(paymentStatus) ? (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {connectedChannels.map((cc) => (
                <ConnectedChannelCard
                  key={cc.id}
                  channel={cc}
                  importing={importing === cc.id}
                  onImport={() => importConnected(cc)}
                />
              ))}
            </div>
          ) : (
            <Paywalled iconOnly>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {connectedChannels.map((cc) => (
                  <ConnectedChannelCard
                    key={cc.id}
                    channel={cc}
                    importing={importing === cc.id}
                    onImport={() => importConnected(cc)}
                  />
                ))}
              </div>
            </Paywalled>
          )}
        </div>
      )}

      {channels.length > 0 && (
        <>
          {/* Toolbar. Same vocabulary as the videos page - a sort
              dimension, a direction, a view toggle - so the two pages
              read as one product rather than two conventions. No
              pagination: four channels do not need paging, and a control
              that does nothing is worse than an absent one. */}
          <div className="flex items-center gap-2 mb-3">
            {/* Scope. Left of everything because it decides what the
                rest of the toolbar even means - the filters, the sort
                dimensions and the empty state all change with it. */}
            <div className="flex">
              <Button
                variant={scope === "channels" ? "default" : "outline"}
                onClick={() => setScope("channels")}
                aria-pressed={scope === "channels"}
              >
                <Tv />
                Channels
              </Button>
              <Button
                variant={scope === "videos" ? "default" : "outline"}
                onClick={() => setScope("videos")}
                aria-pressed={scope === "videos"}
              >
                <VideoIcon />
                Videos
              </Button>
            </div>

            <div className="relative w-56">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 size-4 text-muted-foreground pointer-events-none" />
              <input
                type="text"
                placeholder={
                  scope === "videos" ? "Search videos" : "Search channels"
                }
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="h-9 w-full border border-border bg-transparent pl-9 pr-3 text-sm text-foreground outline-none placeholder:text-muted-foreground focus:border-white focus:bg-white/5"
              />
            </div>

            <Popover>
              <PopoverTrigger asChild>
                <Button variant="outline">
                  <SlidersHorizontal />
                  Filter
                  {(scope === "videos" ? videoFilterCount : activeFilterCount) >
                    0 && (
                    <span className="ml-1 font-mono tabular-nums">
                      {scope === "videos" ? videoFilterCount : activeFilterCount}
                    </span>
                  )}
                </Button>
              </PopoverTrigger>
              <PopoverContent className="w-80">
                {scope === "videos" ? (
                  <VideoFilterPanel
                    prefs={videoPrefs}
                    activeCount={videoFilterCount}
                    onChange={(next) =>
                      setVideoPrefs((prev) => ({ ...prev, ...next }))
                    }
                  />
                ) : (
                <div className="space-y-5">
                  <FilterChips
                    label="Status"
                    options={STATUS_OPTIONS}
                    selected={statusFilter}
                    onToggle={(v) => toggleIn(statusFilter, v, setStatusFilter)}
                  />
                  <FilterChips
                    label="Worker access"
                    options={AUTH_OPTIONS}
                    selected={authFilter}
                    onToggle={(v) => toggleIn(authFilter, v, setAuthFilter)}
                  />
                  <FilterChips
                    label="On YouTube"
                    options={HEALTH_OPTIONS}
                    selected={healthFilter}
                    onToggle={(v) => toggleIn(healthFilter, v, setHealthFilter)}
                  />
                  <div>
                    <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-2">
                      Date added
                    </div>
                    <div className="grid grid-cols-2 gap-2">
                      <div>
                        <label className="text-[10px] text-muted-foreground block mb-1">
                          From
                        </label>
                        <input
                          type="date"
                          value={addedFrom}
                          onChange={(e) => setAddedFrom(e.target.value)}
                          className="h-9 w-full border border-border bg-transparent px-2 text-xs text-foreground outline-none focus:border-white"
                        />
                      </div>
                      <div>
                        <label className="text-[10px] text-muted-foreground block mb-1">
                          To
                        </label>
                        <input
                          type="date"
                          value={addedTo}
                          onChange={(e) => setAddedTo(e.target.value)}
                          className="h-9 w-full border border-border bg-transparent px-2 text-xs text-foreground outline-none focus:border-white"
                        />
                      </div>
                    </div>
                  </div>
                  {activeFilterCount > 0 && (
                    <div className="flex justify-end pt-1">
                      <button
                        type="button"
                        onClick={resetFilters}
                        className="text-xs text-muted-foreground cursor-pointer font-semibold"
                      >
                        Reset filters
                      </button>
                    </div>
                  )}
                </div>
                )}
              </PopoverContent>
            </Popover>

            <div className="ml-auto flex items-center gap-2">
            {scope === "videos" ? (
              <Select
                value={videoPrefs.sortDimension}
                onValueChange={(v) =>
                  setVideoPrefs((p) => ({
                    ...p,
                    sortDimension: v as SortDimension,
                  }))
                }
              >
                <SelectTrigger className="w-44">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(Object.keys(VIDEO_SORT_LABELS) as SortDimension[]).map(
                    (d) => (
                      <SelectItem key={d} value={d}>
                        {VIDEO_SORT_LABELS[d]}
                      </SelectItem>
                    )
                  )}
                </SelectContent>
              </Select>
            ) : (
              <Select
                value={sortDimension}
                onValueChange={(v) =>
                  setSortDimension(v as ChannelSortDimension)
                }
              >
                <SelectTrigger className="w-44">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(
                    Object.keys(CHANNEL_SORT_LABELS) as ChannelSortDimension[]
                  ).map((d) => (
                    <SelectItem key={d} value={d}>
                      {CHANNEL_SORT_LABELS[d]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
            <Button
              variant="outline"
              size="icon"
              onClick={() =>
                scope === "videos"
                  ? setVideoPrefs((p) => ({
                      ...p,
                      sortDirection: p.sortDirection === "asc" ? "desc" : "asc",
                    }))
                  : setSortDirection((d) => (d === "asc" ? "desc" : "asc"))
              }
              title={activeDirection === "asc" ? "Ascending" : "Descending"}
              aria-label="Toggle sort direction"
            >
              {activeDirection === "asc" ? <ArrowUp /> : <ArrowDown />}
            </Button>
            <div className="flex">
              <Button
                variant={activeView === "grid" ? "default" : "outline"}
                size="icon"
                onClick={() => setActiveView("grid")}
                title="Grid"
                aria-label="Grid view"
              >
                <LayoutGrid />
              </Button>
              <Button
                variant={activeView === "list" ? "default" : "outline"}
                size="icon"
                onClick={() => setActiveView("list")}
                title="List"
                aria-label="List view"
              >
                <LayoutList />
              </Button>
            </div>
            </div>
          </div>

          {scope === "videos" ? (
            videosLoading ? (
              <div className="border border-dashed border-border p-8 text-center">
                <p className="text-sm text-muted-foreground">
                  Loading your library...
                </p>
              </div>
            ) : visibleVideos.length === 0 ? (
              <div className="border border-dashed border-border p-8 text-center">
                <p className="text-sm text-muted-foreground">
                  {videos.length === 0
                    ? "Nothing archived yet. Videos appear here as your channels sync."
                    : "No videos match this filter."}
                </p>
              </div>
            ) : activeView === "grid" ? (
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
                {visibleVideos.map((v) => (
                  <VideoCard
                    key={v.id}
                    video={v}
                    settings={globalSettings}
                    channelLabel={v.channelName}
                    onClick={() =>
                      navigate(`/youtube/channel/${v.channelId}?video=${v.id}`)
                    }
                  />
                ))}
              </div>
            ) : (
              <div className="space-y-2">
                {visibleVideos.map((v) => (
                  <VideoCard
                    key={v.id}
                    video={v}
                    variant="list"
                    settings={globalSettings}
                    channelLabel={v.channelName}
                    onClick={() =>
                      navigate(`/youtube/channel/${v.channelId}?video=${v.id}`)
                    }
                  />
                ))}
              </div>
            )
          ) : sortedChannels.length === 0 ? (
            /* Filtered to nothing. Distinct from having no channels at
               all, which is a different screen entirely - saying "no
               channels" here would suggest the archive was empty. */
            <div className="border border-dashed border-border p-8 text-center">
              <p className="text-sm text-muted-foreground">
                No channels match this filter.
              </p>
            </div>
          ) : activeView === "grid" ? (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {sortedChannels.map((c) => (
                <ChannelCard
                  key={c.id}
                  channel={c}
                  onToggleActive={toggleActive}
                />
              ))}
            </div>
          ) : (
            <div className="space-y-2">
              {/* Column headings, list view only. Without them the
                  numbers are just numbers. */}
              <div className="flex items-center gap-4 px-4 text-[10px] uppercase tracking-wider text-muted-foreground font-semibold">
                <span className="flex-1">Channel</span>
                <span className="hidden md:block w-24 text-right">Archived</span>
                <span className="hidden md:block w-20 text-right">Storage</span>
                <span className="hidden lg:block w-24 text-right">Cost</span>
                <span className="hidden lg:block w-28 text-right">Synced</span>
                <span className="w-9" />
              </div>
              {sortedChannels.map((c) => (
                <ChannelListRow
                  key={c.id}
                  channel={c}
                  onToggleActive={toggleActive}
                />
              ))}
            </div>
          )}
        </>
      )}

      {removedChannels.length > 0 && (
        <RemovedChannelsSection
          channels={removedChannels}
          onPurge={purgeRemoved}
        />
      )}

      <YouTubeSettingsPanel
        settings={globalSettings}
        open={settingsOpen}
        onOpenChange={setSettingsOpen}
        onSave={saveGlobalSettings}
      />
    </div>
  )
}

// A channel the user owns (surfaced from their connected account / worker
// app) that isn't tracked yet. One tap imports it via the same track flow
// as pasting a URL, then it moves down into the tracked grid.
function ConnectedChannelCard({
  channel,
  importing,
  onImport,
}: {
  channel: ConnectedChannel
  importing: boolean
  onImport: () => void
}) {
  const title = channel.title || channel.handle || "Your channel"
  const handleLabel = channel.handle
    ? channel.handle.startsWith("@")
      ? channel.handle
      : `@${channel.handle}`
    : null
  return (
    <div className="flex items-center gap-3 rounded-lg border bg-card p-3">
      {channel.thumbnailUrl ? (
        <img
          draggable={false}
          src={channel.thumbnailUrl}
          alt=""
          className="size-10 rounded-full object-cover shrink-0"
        />
      ) : (
        <div className="size-10 rounded-full bg-muted shrink-0" />
      )}
      <div className="min-w-0 flex-1">
        <div className="font-semibold text-sm truncate">{title}</div>
        {handleLabel && (
          <div className="text-xs text-muted-foreground truncate font-mono">
            {handleLabel}
          </div>
        )}
      </div>
      <Button size="sm" onClick={onImport} disabled={importing}>
        {importing ? "Importing…" : "Import"}
      </Button>
    </div>
  )
}

// Channels the user removed that are still inside the 30-day grace window.
// They're kept (not billed) so re-adding a channel restores it; this section
// also lets the user wipe one immediately to free the storage — with a hard,
// irreversible confirmation before anything is deleted.
function RemovedChannelsSection({
  channels,
  onPurge,
}: {
  channels: RemovedChannel[]
  onPurge: (id: string) => Promise<void>
}) {
  const { toast } = useToast()
  const [target, setTarget] = React.useState<RemovedChannel | null>(null)
  const [busy, setBusy] = React.useState(false)

  const confirmPurge = async () => {
    if (!target) return
    setBusy(true)
    try {
      await onPurge(target.id)
      setTarget(null)
      toast({ title: "Channel deleted" })
    } catch (e) {
      toast({
        title: "Couldn't delete the archive",
        description: e instanceof Error ? e.message : "Please try again.",
        variant: "error",
      })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mb-8">
      <h2 className="text-sm font-semibold mb-3">Recently removed</h2>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {channels.map((c) => (
          <RemovedChannelCard
            key={c.id}
            channel={c}
            onDelete={() => setTarget(c)}
          />
        ))}
      </div>

      <Dialog
        open={target !== null}
        onOpenChange={(o) => !busy && !o && setTarget(null)}
      >
        <DialogContent className="max-w-md">
          <div className="p-6 space-y-5">
            <DialogHeader>
              <DialogTitle>Delete this archive permanently?</DialogTitle>
            </DialogHeader>
            <p className="text-sm text-muted-foreground leading-relaxed">
              <strong>{target?.name}</strong> and everything we've stored for it
              will be erased right now. This can't be undone.
            </p>
            <DialogFooter>
              <Button
                variant="outline"
                onClick={() => setTarget(null)}
                disabled={busy}
              >
                Keep it
              </Button>
              <Button
                variant="destructive"
                onClick={confirmPurge}
                disabled={busy}
              >
                {busy ? "…" : "Delete permanently"}
              </Button>
            </DialogFooter>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}

// One removed channel: dimmed avatar, name/handle, and how long until the
// automatic purge, plus the red "Delete permanently" action.
function RemovedChannelCard({
  channel,
  onDelete,
}: {
  channel: RemovedChannel
  onDelete: () => void
}) {
  const title = channel.name || channel.handle || "Channel"
  const handleLabel = channel.handle
    ? channel.handle.startsWith("@")
      ? channel.handle
      : `@${channel.handle}`
    : null
  const purgeLabel = channel.purgeAt
    ? `Deletes in ${formatTimeUntil(channel.purgeAt)}`
    : "Kept for 30 days"
  return (
    <div className="flex items-center gap-3 rounded-lg border bg-card p-3">
      {channel.avatarUrl ? (
        <img
          draggable={false}
          src={channel.avatarUrl}
          alt=""
          className="size-10 rounded-full object-cover shrink-0 opacity-50"
        />
      ) : (
        <div className="size-10 rounded-full bg-muted shrink-0" />
      )}
      <div className="min-w-0 flex-1">
        <div className="font-semibold text-sm truncate">{title}</div>
        <div className="text-xs text-muted-foreground truncate font-mono">
          {handleLabel ? `${handleLabel} · ` : ""}
          {purgeLabel}
        </div>
      </div>
      <Button
        size="sm"
        variant="outline"
        onClick={onDelete}
        className="shrink-0 border-destructive text-destructive hover:bg-destructive hover:text-destructive-foreground"
      >
        Delete permanently
      </Button>
    </div>
  )
}
