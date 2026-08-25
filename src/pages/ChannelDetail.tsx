import * as React from "react"
import {
  useParams,
  useNavigate,
  useSearchParams,
  Link,
  Navigate,
} from "react-router-dom"
import {
  Activity,
  AlertTriangle,
  Archive,
  ArrowDown,
  ArrowLeft,
  ArrowUp,
  ChevronLeft,
  ChevronRight,
  Clock,
  DollarSign,
  Download,
  ExternalLink,
  Eye,
  HardDrive,
  LayoutGrid,
  LayoutList,
  MessageSquare,
  RefreshCw,
  Search,
  Settings,
  SlidersHorizontal,
  Trash2,
  Users,
  Video as VideoIcon,
  X,
} from "lucide-react"
import { DeletedCommentsSheet } from "@/components/DeletedCommentsSheet"
import { DownloadPanel } from "@/components/DownloadPanel"
import { SyncPanel } from "@/components/SyncPanel"
import type { SyncOptions } from "@/components/SyncPanel"
import { Button } from "@/components/ui/button"
import { Switch } from "@/components/ui/switch"
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs"
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
import { VideoCard } from "@/components/VideoCard"
import { VideoDetailPanel } from "@/components/VideoDetailPanel"
import { ChannelAvatar } from "@/components/ChannelAvatar"
import { ChannelSettingsPanel } from "@/components/ChannelSettingsPanel"
// One shared list + label map, not a hand-copied one per surface. Three
// copies of the same options is how the filter chips drift apart.
import {
  VISIBILITY_OPTIONS,
  VISIBILITY_LABELS,
} from "@/components/PresetEditor"
import { EmptyState } from "@/components/EmptyState"
import type {
  Channel,
  ChannelArchiveSettings,
  SortDimension,
  SortDirection,
  Video,
  VideoType,
  VideoVisibility,
} from "@/lib/types"
import {
  formatBytes,
  formatCount,
  formatMonthYear,
  formatRelativeDate,
  formatTotalDuration,
  formatMonthlyCost,
} from "@/lib/format"
import { abbreviateCountry } from "@/lib/countries"
import {
  estimateMonthlyStorageCostUsd,
  isQualityOutdated,
} from "@/lib/estimates"
import { normalizeChannelSettings } from "@/lib/mockData"
import { usePrices } from "@/lib/pricing"
import { cn } from "@/lib/utils"
import { useWorkerStatus } from "@/lib/workerStatus"
import { usePanelParam } from "@/lib/usePanelParam"
import { useDocumentTitle } from "@/lib/useDocumentTitle"
import { useToast } from "@/components/ui/toast"

const SORT_DIMENSION_LABELS: Record<SortDimension, string> = {
  upload: "Upload date",
  views: "Views",
  filesize: "File size",
  duration: "Duration",
}

const TYPE_OPTIONS: { value: VideoType; label: string }[] = [
  { value: "video", label: "Video" },
  { value: "short", label: "Short" },
  { value: "livestream", label: "Livestream" },
]

function videoVisibility(v: Video): VideoVisibility {
  if (v.status === "deleted_on_youtube") return "deleted"
  // One backend writer still stores members-only as "members_only", which
  // matches no chip: the card shows a Members badge and then the row vanishes
  // the moment you filter by Members. Cast because the stray spelling sits
  // outside the declared union (VideoCard tolerates it the same way).
  if ((v.privacy as string) === "members_only") return "members"
  return v.privacy
}

export default function ChannelDetail() {
  const { channelId } = useParams<{ channelId: string }>()
  const [channel, setChannel] = React.useState<Channel | null>(null)
  const [channelLoading, setChannelLoading] = React.useState(true)
  // Tab title tracks the channel name once it's loaded. Channel
  // identifier as a fallback for the loading flash.
  useDocumentTitle(channel?.name || channel?.handle || channelId || "Channel")

  React.useEffect(() => {
    if (!channelId) {
      setChannelLoading(false)
      return
    }
    let cancelled = false
    setChannelLoading(true)
    fetch(`/api/youtube/channels/${encodeURIComponent(channelId)}`, {
      credentials: "include",
    })
      .then((res) => {
        if (cancelled) return null
        if (res.status === 404) return null
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then((data) => {
        if (cancelled) return
        if (data) {
          const ch = data as Channel
          setChannel({
            ...ch,
            settings: normalizeChannelSettings(ch.settings),
          })
        } else {
          setChannel(null)
        }
        setChannelLoading(false)
      })
      .catch(() => {
        if (cancelled) return
        setChannel(null)
        setChannelLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [channelId])

  if (channelLoading) {
    return (
      <div className="p-8">
        <div className="text-sm text-muted-foreground font-semibold">
          Loading channel…
        </div>
      </div>
    )
  }

  if (!channelId || !channel) {
    // A channel that isn't in the library - never was, or was just removed -
    // is a dead end, not a destination. Bounce straight to the YouTube page
    // instead of parking the user on an error card whose only action goes
    // there anyway. This only fires after the load finishes (the loading
    // guard above returns first), so it never flashes mid-fetch. `replace`
    // so Back doesn't return them to the missing URL.
    return <Navigate to="/youtube" replace />
  }

  return (
    <ChannelDetailContent
      channelId={channelId}
      channel={channel}
      setChannel={setChannel}
    />
  )
}

function ChannelDetailContent({
  channelId,
  channel,
  setChannel,
}: {
  channelId: string
  channel: Channel
  setChannel: React.Dispatch<React.SetStateAction<Channel | null>>
}) {
  const navigate = useNavigate()

  const [videos, setVideos] = React.useState<Video[]>([])
  const [videosLoading, setVideosLoading] = React.useState(true)
  // Account-wide display preferences. Video previews and filter presets
  // are one setting for the whole archive, not per channel, so they are
  // read from here rather than from this channel's copy.
  const [globalSettings, setGlobalSettings] =
    React.useState<ChannelArchiveSettings | null>(null)
  React.useEffect(() => {
    let cancelled = false
    fetch("/api/youtube/settings", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (!cancelled && d)
          setGlobalSettings(
            normalizeChannelSettings(d as Partial<ChannelArchiveSettings>)
          )
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [])

  const saveGlobalSettings = async (next: ChannelArchiveSettings) => {
    const res = await fetch("/api/youtube/settings", {
      method: "PUT",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(next),
    }).catch(() => null)
    if (!res || !res.ok) {
      toast({
        title: "Couldn't save display settings",
        description: res ? `Server returned ${res.status}.` : "Backend unreachable.",
        variant: "error",
      })
      throw new Error("global save failed")
    }
    setGlobalSettings(next)
  }
  const prices = usePrices()

  // Load discovered videos for this channel from the backend.
  //
  // Cursor-paginated under the hood. We fetch the first page eagerly,
  // then chain subsequent pages back-to-back so the full set ends up
  // loaded without the user clicking through pages. The pagination
  // exists for two reasons:
  //
  //   1) The response body is bounded per request (used to be ~10 MB
  //      for a 5k-video channel; now it's at most ~limit*1 KB).
  //   2) The server no longer presigns a thumbnail R2 URL per row -
  //      that was 1 Class B op per video per page-load. We bulk-fetch
  //      presigned URLs per page (see _fetchThumbnailUrls below) so
  //      thumbnails only get minted for what we actually receive.
  //
  // We use a server-side sort of (uploadDate DESC, video_id ASC), so
  // the client-side sort for "Upload date" + descending is now a no-op.
  // Other sort dimensions (views, filesize, duration) still run client-
  // side over the loaded set.
  React.useEffect(() => {
    let cancelled = false
    setVideosLoading(true)
    setVideos([])

    const loadPage = async (cursor: string | null) => {
      const url = new URL(
        `/api/youtube/channels/${encodeURIComponent(channelId)}/videos`,
        window.location.origin
      )
      if (cursor) url.searchParams.set("cursor", cursor)
      url.searchParams.set("limit", "200")
      const res = await fetch(url.toString(), { credentials: "include" })
      if (!res.ok) return null
      const body = (await res.json()) as {
        items: Video[]
        nextCursor: string | null
      }
      if (!body || !Array.isArray(body.items)) return null
      return body
    }

    const fetchThumbnailUrls = async (
      videoIds: string[]
    ): Promise<Record<string, string>> => {
      if (videoIds.length === 0) return {}
      try {
        const res = await fetch(
          `/api/youtube/channels/${encodeURIComponent(channelId)}/thumbnail-urls`,
          {
            method: "POST",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ video_ids: videoIds }),
          }
        )
        if (!res.ok) return {}
        const body = (await res.json()) as { urls?: Record<string, string> }
        return body.urls ?? {}
      } catch {
        return {}
      }
    }

    void (async () => {
      let cursor: string | null = null
      let firstPage = true
      // Loop until the server says there's no nextCursor. Each
      // iteration appends the new page's items to state so the UI
      // can render incrementally - the user sees the newest 200
      // videos almost immediately even on a channel with thousands
      // of rows.
      // eslint-disable-next-line no-constant-condition
      while (true) {
        if (cancelled) return
        const body: { items: Video[]; nextCursor: string | null } | null =
          await loadPage(cursor)
        if (cancelled) return
        if (!body) break
        const ids = body.items.map((v) => v.id)
        const urls = await fetchThumbnailUrls(ids)
        if (cancelled) return
        // ONLY our own archived thumbnail. This used to fall back to
        // v.thumbnailUrl, which is YouTube's CDN url (i.ytimg.com), so a
        // channel we had not backed up yet still rendered a full grid of
        // images - on loan from the very service the archive exists to
        // outlive. If YouTube went down, so did the page. An image is
        // shown now only when the bytes are ours; everything else falls
        // through to VideoCard's no-thumbnail layout, which already
        // exists and reads as deliberate.
        const hydrated: Video[] = body.items.map((v) => ({
          ...v,
          thumbnailUrl: urls[v.id] ?? "",
        }))
        setVideos((prev) => (firstPage ? hydrated : [...prev, ...hydrated]))
        firstPage = false
        if (!body.nextCursor) break
        cursor = body.nextCursor
      }
      if (!cancelled) setVideosLoading(false)
    })()

    return () => {
      cancelled = true
    }
  }, [channelId])
  // Detail-panel state lives in the URL (?video=<id>) so a refresh
  // keeps the panel open and the URL is shareable.
  const [searchParams, setSearchParams] = useSearchParams()
  const detailVideoId = searchParams.get("video")
  const detailVideo = React.useMemo<Video | null>(
    () => (detailVideoId ? videos.find((v) => v.id === detailVideoId) ?? null : null),
    [detailVideoId, videos]
  )
  const setDetailVideo = React.useCallback(
    (v: Video | null) => {
      setSearchParams(
        (curr) => {
          const next = new URLSearchParams(curr)
          if (v) next.set("video", v.id)
          else next.delete("video")
          return next
        },
        { replace: false }
      )
    },
    [setSearchParams]
  )
  // Panel open/close state lives in the URL (?panel=settings|sync|download)
  // so refresh keeps whichever sheet was open. Only one panel can be
  // open at a time via this scheme - opening another overwrites the
  // param. The video detail panel uses its own ?video=<id> param so
  // it composes independently with these.
  const [settingsOpen, setSettingsOpen] = usePanelParam("settings")
  const [downloadPanelOpen, setDownloadPanelOpen] = usePanelParam("download")
  const [deletedCommentsOpen, setDeletedCommentsOpen] = usePanelParam("deleted-comments")
  // Mirrors DownloadPanel's internal busy flag so the Download-trigger
  // button on this page can animate / relabel itself while a bulk run
  // is in flight (even if the sheet is closed).
  const [downloadBusy, setDownloadBusy] = React.useState(false)
  const [syncPanelOpen, setSyncPanelOpen] = usePanelParam("sync")
  const [selectedVideoIds, setSelectedVideoIds] = React.useState<Set<string>>(
    new Set()
  )
  // For shift-click range selection: the anchor (last plain-clicked
  // video) and the latest displayed order, both read at click time.
  const selectAnchorRef = React.useRef<string | null>(null)
  const pageVideosRef = React.useRef<Video[]>([])

  const toggleSelect = React.useCallback((v: Video, shiftKey: boolean) => {
    const anchorId = selectAnchorRef.current
    const order = pageVideosRef.current
    const a = anchorId ? order.findIndex((x) => x.id === anchorId) : -1
    const b = order.findIndex((x) => x.id === v.id)
    const doRange = shiftKey && a !== -1 && b !== -1 && anchorId !== v.id

    setSelectedVideoIds((prev) => {
      const next = new Set(prev)
      if (doRange) {
        // Select everything between the anchor and this video in the
        // displayed order (Finder-style shift-click range), additive.
        const [lo, hi] = a < b ? [a, b] : [b, a]
        for (let i = lo; i <= hi; i++) next.add(order[i].id)
      } else if (next.has(v.id)) {
        next.delete(v.id)
      } else {
        next.add(v.id)
      }
      return next
    })
    // Keep the anchor through a range; on a plain toggle, set it here so
    // the next shift-click ranges from this click.
    if (!doRange) selectAnchorRef.current = v.id
  }, [])

  // Click on a video card always opens the side detail panel. The Play
  // button inside the panel branches: synced video plays in-app, unsynced
  // pops the YouTube watch page in a new tab.
  const openVideo = React.useCallback(
    (v: Video) => {
      setDetailVideo(v)
    },
    [setDetailVideo]
  )

  // View mode (grid vs list) — persisted per user.
  const [viewMode, setViewMode] = React.useState<"grid" | "list">(() => {
    if (typeof window === "undefined") return "grid"
    return window.localStorage.getItem("archive336.videoViewMode") === "list"
      ? "list"
      : "grid"
  })
  React.useEffect(() => {
    window.localStorage.setItem("archive336.videoViewMode", viewMode)
  }, [viewMode])

  // Pagination — 30 per page in both views (3×10 grid, 30 list rows).
  const pageSize = 30
  const [currentPage, setCurrentPage] = React.useState(1)
  const [settings, setSettings] = React.useState<ChannelArchiveSettings | null>(
    channel?.settings ?? null
  )
  const [syncing, setSyncing] = React.useState(false)
  const { toast } = useToast()

  const [search, setSearch] = React.useState("")
  const [sortDimension, setSortDimension] = React.useState<SortDimension>("upload")
  const [sortDirection, setSortDirection] = React.useState<SortDirection>("desc")
  const [dateFrom, setDateFrom] = React.useState("")
  const [dateTo, setDateTo] = React.useState("")
  const [visibilityFilter, setVisibilityFilter] = React.useState<Set<VideoVisibility>>(
    new Set()
  )
  const [typeFilter, setTypeFilter] = React.useState<Set<VideoType>>(new Set())
  const [activePresetId, setActivePresetId] = React.useState<string>("all")

  const activeFilterCount =
    (dateFrom ? 1 : 0) +
    (dateTo ? 1 : 0) +
    visibilityFilter.size +
    typeFilter.size

  const toggleVisibility = (v: VideoVisibility) => {
    setVisibilityFilter((prev) => {
      const next = new Set(prev)
      if (next.has(v)) next.delete(v)
      else next.add(v)
      return next
    })
  }

  const toggleType = (t: VideoType) => {
    setTypeFilter((prev) => {
      const next = new Set(prev)
      if (next.has(t)) next.delete(t)
      else next.add(t)
      return next
    })
  }

  const resetFilters = () => {
    setDateFrom("")
    setDateTo("")
    setVisibilityFilter(new Set())
    setTypeFilter(new Set())
  }

  // "Saved" = any local copy on disk (regular archived + rescued-from-deletion).
  const savedCount = videos.filter(
    (v) =>
      v.status === "archived" ||
      (v.status === "deleted_on_youtube" && v.localPath !== null)
  ).length
  // (Previously had an archivedCount = preserved-after-deletion stat
  // here. Removed when the channel header was reworked: "Saved" was
  // renamed to "Archived" and the old "Archived" slot became the
  // monthly Cost estimate. Per-video deleted-with-local rescue state
  // still surfaces via the Visibility badge on each card.)

  // Fall back through: latest video upload → last channel sync → when the
  // channel was added to the dashboard (so freshly added channels with no
  // videos still show something meaningful).
  const latestUploadISO =
    videos.length > 0
      ? videos.reduce(
          (acc, v) => (v.uploadDate > acc ? v.uploadDate : acc),
          videos[0].uploadDate
        )
      : channel.lastSyncedAt || channel.addedAt
  const totalArchivedBytes = videos.reduce(
    (acc, v) => acc + (v.fileSizeBytes ?? 0),
    0
  )

  const totalArchivedDuration = videos.reduce((acc, v) => {
    if (
      v.status === "archived" ||
      (v.status === "deleted_on_youtube" && v.localPath)
    ) {
      return acc + v.durationSec
    }
    return acc
  }, 0)

  const channelUrl = channel.handle.startsWith("@")
    ? `https://www.youtube.com/${channel.handle}`
    : `https://www.youtube.com/channel/${channel.id}`

  const processed = React.useMemo(() => {
    let result = videos

    const q = search.trim().toLowerCase()
    if (q) {
      result = result.filter(
        (v) =>
          v.title.toLowerCase().includes(q) ||
          v.description.toLowerCase().includes(q)
      )
    }

    if (dateFrom) {
      const from = new Date(dateFrom).getTime()
      if (!isNaN(from)) {
        result = result.filter((v) => new Date(v.uploadDate).getTime() >= from)
      }
    }
    if (dateTo) {
      const to = new Date(dateTo).getTime() + 86_400_000 // inclusive end of day
      if (!isNaN(to)) {
        result = result.filter((v) => new Date(v.uploadDate).getTime() < to)
      }
    }

    if (visibilityFilter.size > 0) {
      result = result.filter((v) => visibilityFilter.has(videoVisibility(v)))
    }

    if (typeFilter.size > 0) {
      result = result.filter((v) => typeFilter.has(v.type))
    }

    // Note: the server already sorts by (uploadDate DESC, video_id ASC),
    // so for sortDimension="upload" + sortDirection="desc" this client
    // sort is effectively a no-op (an O(n) comparison pass on an
    // already-sorted array). Kept for the other sort dimensions
    // (views/filesize/duration), which run client-side over whatever's
    // currently loaded.
    const sorted = [...result]
    const dir = sortDirection === "asc" ? 1 : -1
    sorted.sort((a, b) => {
      let cmp = 0
      switch (sortDimension) {
        case "upload":
          cmp =
            new Date(a.uploadDate).getTime() -
            new Date(b.uploadDate).getTime()
          break
        case "views":
          cmp = a.viewCount - b.viewCount
          break
        case "filesize":
          cmp = (a.fileSizeBytes ?? 0) - (b.fileSizeBytes ?? 0)
          break
        case "duration":
          cmp = a.durationSec - b.durationSec
          break
      }
      return dir * cmp
    })
    return sorted
  }, [videos, search, dateFrom, dateTo, visibilityFilter, typeFilter, sortDimension, sortDirection])

  // What the video list actually renders with.
  //
  // Archiving settings are this channel's; display settings are the
  // account's. Merging here means one stored copy drives every channel
  // at once, which is what the "Global settings" heading in the panel
  // has always claimed and never did - the controls under it wrote
  // per-channel, so four channels could each show different meta fields
  // while every one of them called the setting global.
  const displaySettings = React.useMemo<ChannelArchiveSettings | null>(() => {
    if (!settings) return null
    if (!globalSettings) return settings
    return {
      ...settings,
      showStatusBadges: globalSettings.showStatusBadges,
      useStatusColorBorder: globalSettings.useStatusColorBorder,
      cardMetaFields: globalSettings.cardMetaFields,
      filterPresets: globalSettings.filterPresets,
    }
  }, [settings, globalSettings])

  const presetCounts = React.useMemo(() => {
    const counts: Record<string, number> = {}
    const presets = displaySettings?.filterPresets ?? []
    for (const p of presets) {
      let result = videos
      const q = p.search.trim().toLowerCase()
      if (q) {
        result = result.filter(
          (v) =>
            v.title.toLowerCase().includes(q) ||
            v.description.toLowerCase().includes(q)
        )
      }
      if (p.dateFrom) {
        const from = new Date(p.dateFrom).getTime()
        if (!isNaN(from)) {
          result = result.filter((v) => new Date(v.uploadDate).getTime() >= from)
        }
      }
      if (p.dateTo) {
        const to = new Date(p.dateTo).getTime() + 86_400_000
        if (!isNaN(to)) {
          result = result.filter((v) => new Date(v.uploadDate).getTime() < to)
        }
      }
      if (p.visibilities.length > 0) {
        const set = new Set(p.visibilities)
        result = result.filter((v) => set.has(videoVisibility(v)))
      }
      if (p.types.length > 0) {
        const set = new Set(p.types)
        result = result.filter((v) => set.has(v.type))
      }
      counts[p.id] = result.length
    }
    return counts
  }, [videos, displaySettings?.filterPresets])

  const applyPreset = React.useCallback(
    (id: string) => {
      const preset = settings?.filterPresets.find((p) => p.id === id)
      if (!preset) return
      setActivePresetId(id)
      setSearch(preset.search)
      setDateFrom(preset.dateFrom)
      setDateTo(preset.dateTo)
      setVisibilityFilter(new Set(preset.visibilities))
      setTypeFilter(new Set(preset.types))
      setSortDimension(preset.sortDimension)
      setSortDirection(preset.sortDirection)
      setViewMode(preset.viewMode)
    },
    [settings?.filterPresets]
  )

  // If the active preset gets deleted (e.g. via settings panel), fall back to "all".
  React.useEffect(() => {
    if (!settings) return
    const exists = settings.filterPresets.some((p) => p.id === activePresetId)
    if (!exists) applyPreset("all")
  }, [settings, activePresetId, applyPreset])

  const isFilterActive =
    search.trim().length > 0 || activeFilterCount > 0

  // Turning a paused channel back on, from the header.
  //
  // A disabled Sync button is a dead end: it says no and offers nothing.
  // The one thing that would make it work is the Active switch, buried in
  // the settings panel, so the toggle takes the button's place until it
  // is on and then hands the space back.
  const [activating, setActivating] = React.useState(false)
  const activateChannel = async () => {
    if (!settings || activating) return
    setActivating(true)
    const next = { ...settings, active: true }
    try {
      const res = await fetch(
        `/api/youtube/channels/${encodeURIComponent(channelId)}`,
        {
          method: "PUT",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...channel, settings: next }),
        }
      ).catch(() => null)
      if (!res || !res.ok) {
        toast({
          title: "Couldn't activate this channel",
          description: res ? `Server returned ${res.status}.` : "Backend unreachable.",
          variant: "error",
        })
        return
      }
      const updated = (await res.json()) as Channel
      setSettings(next)
      setChannel(updated)
      toast({ title: "Channel active - queued videos will resume" })
      void refetchVideos()
    } finally {
      setActivating(false)
    }
  }

  // What an empty list actually means.
  //
  // "No videos" used to be the answer to every cause, which made it flatly
  // wrong in the most common one. A freshly tracked channel has not been
  // scanned yet, so it reported "No videos" while YouTube's own count sat
  // three inches above it reading 499. For a backup product that is the
  // worst possible lie: it is indistinguishable from "we looked, there is
  // nothing to protect".
  //
  // The list is built by the worker on the user's own machine, not here,
  // so "we have not been told yet" is a real and common state that
  // deserves saying out loud rather than being rendered as absence.
  const emptyStateFor = (presetLabel: string) => {
    if (isFilterActive)
      return {
        title: "No matches",
        description: "No videos match the current search or filters.",
      }
    if (videos.length > 0)
      return {
        title: "Nothing in this view",
        description: `No videos in the ${presetLabel} preset.`,
      }
    // Title only. The explanation that used to sit here named the worker
    // and quoted YouTube's count back at the user, which is the mechanism
    // rather than the answer - and the count is already on this page.
    if ((channel.videoCount ?? 0) > 0)
      return { title: "Waiting for the first scan" }
    return {
      title: "This channel has no videos",
      description: "Nothing to back up yet. New uploads are captured automatically.",
    }
  }

  // Reset to page 1 whenever the result set shifts.
  React.useEffect(() => {
    setCurrentPage(1)
  }, [
    search,
    dateFrom,
    dateTo,
    visibilityFilter,
    typeFilter,
    sortDimension,
    sortDirection,
    activePresetId,
  ])

  const totalPages = Math.max(1, Math.ceil(processed.length / pageSize))
  const safePage = Math.min(currentPage, totalPages)
  const pageStart = (safePage - 1) * pageSize
  const pageVideos = processed.slice(pageStart, pageStart + pageSize)

  // Keep the order ref current for shift-click range selection (read in
  // toggleSelect at click time).
  React.useEffect(() => {
    pageVideosRef.current = pageVideos
  }, [pageVideos])

  // Refetches the video list from the server. Used after a sync job
  // completes to pick up status changes the worker wrote into the row.
  //
  // Walks all cursor pages back-to-back so the local 'videos' state
  // ends up consistent with what's in the DB. Bulk-presigns thumbnails
  // per page (same pattern as the initial mount effect) so we don't
  // burn one Class B op per thumbnail.
  const refetchVideos = React.useCallback(async () => {
    const collected: Video[] = []
    let cursor: string | null = null
    // eslint-disable-next-line no-constant-condition
    while (true) {
      const url = new URL(
        `/api/youtube/channels/${encodeURIComponent(channelId)}/videos`,
        window.location.origin
      )
      if (cursor) url.searchParams.set("cursor", cursor)
      url.searchParams.set("limit", "200")
      const res = await fetch(url.toString(), { credentials: "include" })
      if (!res.ok) return
      const body = (await res.json()) as {
        items: Video[]
        nextCursor: string | null
      }
      if (!body || !Array.isArray(body.items)) return
      const ids = body.items.map((v) => v.id)
      let urls: Record<string, string> = {}
      try {
        const presignRes = await fetch(
          `/api/youtube/channels/${encodeURIComponent(channelId)}/thumbnail-urls`,
          {
            method: "POST",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ video_ids: ids }),
          }
        )
        if (presignRes.ok) {
          const pBody = (await presignRes.json()) as {
            urls?: Record<string, string>
          }
          urls = pBody.urls ?? {}
        }
      } catch {
        // Tolerate presign failure - rendering a blank thumbnail
        // beats a hard 500 on the whole refetch path.
      }
      for (const v of body.items) {
        collected.push({
          ...v,
          thumbnailUrl: urls[v.id] ?? "",
        })
      }
      if (!body.nextCursor) break
      cursor = body.nextCursor
    }
    setVideos(collected)
  }, [channelId])

  // Enqueue a real backend sync job. The desktop app (when running) claims
  // it, runs yt-dlp, uploads to R2, and the server flips the video row to
  // status='archived'. We surface progress via the polling effect below.
  const handleSync = async (v: Video) => {
    setVideos((curr) =>
      curr.map((x) =>
        x.id === v.id ? { ...x, status: "syncing", syncProgress: 0 } : x
      )
    )
    try {
      const res = await fetch(
        `/api/youtube/channels/${encodeURIComponent(channelId)}/sync-files`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify({ video_ids: [v.id] }),
        }
      )
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
    } catch (e) {
      // Revert optimistic state on failure
      setVideos((curr) =>
        curr.map((x) =>
          x.id === v.id
            ? { ...x, status: "discovered", syncProgress: undefined }
            : x
        )
      )
      toast({
        title: "Couldn't queue sync",
        description: e instanceof Error ? e.message : "Unknown error",
        variant: "error",
      })
    }
  }

  const handleRetry = (v: Video) => {
    setVideos((curr) =>
      curr.map((x) => (x.id === v.id ? { ...x, status: "discovered" } : x))
    )
    void handleSync(v)
  }

// Poll active sync jobs while any video is in 'syncing' state. Each tick
  // updates syncProgress on matching videos. When a job disappears from
  // the active list, it's done (or failed) — refetch the video list so
  // the row picks up whatever final status the worker wrote.
  //
  // Subtle race: a freshly-clicked video transitions to "syncing" locally
  // BEFORE the POST /sync-files request lands on the server. If a poll
  // tick runs in that window, we'd see "video syncing, no active job"
  // and incorrectly conclude the job completed, refetch, and clobber the
  // optimistic state with the still-discovered DB row. Guard: only treat
  // a missing job as "completed" if we've previously seen it active.
  const anySyncing = videos.some((v) => v.status === "syncing")
  // Poll the worker-status endpoint whenever there's a video in
  // syncing state. UI flips between "Syncing X%" and "Worker app
  // inactive — waiting…" based on this so the user understands why
  // a freshly-queued job is sitting at 0%.
  const workerStatus = useWorkerStatus(anySyncing)
  React.useEffect(() => {
    if (!anySyncing) return
    const seenJobVideoIds = new Set<string>()
    let cancelled = false

    const tick = async () => {
      try {
        const res = await fetch("/api/youtube/sync-jobs/active", {
          credentials: "include",
        })
        if (!res.ok || cancelled) return
        const jobs = (await res.json()) as Array<{
          videoId: string
          kind?: string | null
          progress: number
          status: string
        }>
        // Only file downloads belong in this map. Captions and metadata
        // jobs are upkeep on videos we already hold - the user never
        // asked for them, so a progress bar for one would be claiming a
        // download is running when none is. Keying by video id alone
        // also let the two kinds mask each other: a metadata job queued
        // after a download would take the row's progress bar, and one
        // still pending after the download finished would hide the
        // finish, pinning the row at "syncing" forever. Jobs written
        // before the kind column existed carry no kind; those are
        // downloads.
        const downloads = jobs.filter((j) => (j.kind ?? "video") === "video")
        const byId = new Map(downloads.map((j) => [j.videoId, j]))
        for (const j of downloads) seenJobVideoIds.add(j.videoId)

        let needsRefetch = false
        setVideos((curr) =>
          curr.map((v) => {
            if (v.status !== "syncing") return v
            const job = byId.get(v.id)
            if (job) {
              return { ...v, syncProgress: job.progress }
            }
            // Only refetch if we'd PREVIOUSLY seen this video as an
            // active job. Otherwise this is the just-enqueued race —
            // the job exists on the server but our tick raced ahead of
            // the POST landing.
            if (seenJobVideoIds.has(v.id)) {
              seenJobVideoIds.delete(v.id)
              needsRefetch = true
            }
            return v
          })
        )
        if (needsRefetch) void refetchVideos()
      } catch {
        // Network blip — try again next tick.
      }
    }

    void tick()
    const interval = window.setInterval(tick, 3000)
    return () => {
      cancelled = true
      window.clearInterval(interval)
    }
  }, [anySyncing, refetchVideos])

  // Synchronous-ish variant of refetchVideos used by runSync below -
  // returns the freshly-loaded list so the caller can immediately
  // inspect it (e.g. compute which videos still need to be enqueued
  // for download). Walks all pages the same way refetchVideos does.
  const refetchVideosFromServer = async (): Promise<Video[] | null> => {
    try {
      const collected: Video[] = []
      let cursor: string | null = null
      // eslint-disable-next-line no-constant-condition
      while (true) {
        const url = new URL(
          `/api/youtube/channels/${encodeURIComponent(channelId)}/videos`,
          window.location.origin
        )
        if (cursor) url.searchParams.set("cursor", cursor)
        url.searchParams.set("limit", "200")
        const vidRes = await fetch(url.toString(), { credentials: "include" })
        if (!vidRes.ok) return null
        const body = (await vidRes.json()) as {
          items: Video[]
          nextCursor: string | null
        }
        if (!body || !Array.isArray(body.items)) return null
        const ids = body.items.map((v) => v.id)
        let urls: Record<string, string> = {}
        try {
          const pres = await fetch(
            `/api/youtube/channels/${encodeURIComponent(channelId)}/thumbnail-urls`,
            {
              method: "POST",
              credentials: "include",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ video_ids: ids }),
            }
          )
          if (pres.ok) {
            const pBody = (await pres.json()) as {
              urls?: Record<string, string>
            }
            urls = pBody.urls ?? {}
          }
        } catch {
          // Same tolerance as elsewhere - presign blip shouldn't kill
          // the whole sync flow.
        }
        for (const v of body.items) {
          collected.push({
            ...v,
            thumbnailUrl: urls[v.id] ?? "",
          })
        }
        if (!body.nextCursor) break
        cursor = body.nextCursor
      }
      setVideos(collected)
      return collected
    } catch {
      return null
    }
  }

  const runSync = async (options: SyncOptions) => {
    setSyncing(true)
    // Failures from any step get collected here and surfaced as a
    // single toast at the end. Each step continues on its own
    // failure - we want the user to get partial work done rather
    // than blocking the whole flow on one bad step.
    const failures: string[] = []
    try {
      if (options.videos) {
        // Step 1: server-side OAuth catalog reconciliation. Inserts
        // any new uploads, updates existing rows, marks deleted ones.
        try {
          const res = await fetch(
            `/api/youtube/channels/${encodeURIComponent(channelId)}/sync`,
            { method: "POST", credentials: "include" }
          )
          if (!res.ok) {
            failures.push(
              res.status === 502
                ? "Couldn't reach YouTube to check for new uploads."
                : `Discovery returned ${res.status}.`
            )
          }
        } catch {
          failures.push("Couldn't reach the server for new-upload check.")
        }

        // Step 2: re-enqueue any sync jobs left in 'failed' state.
        // Server resets the matching UserChannelVideo rows from
        // 'failed' back to 'discovered' before creating the new
        // pending sync_jobs.
        try {
          await fetch("/api/youtube/sync-jobs/retry-failed", {
            method: "POST",
            credentials: "include",
          })
        } catch {
          // Non-fatal - if there's nothing to retry, this is just a
          // wasted RPC. No toast needed.
        }

        // Refetch the live video list so we pick up newly-discovered
        // rows AND any rows the retry-failed endpoint just flipped
        // from 'failed' back to 'discovered'.
        const fresh = await refetchVideosFromServer()
        const liveVideos = fresh ?? videos

        // Step 3: bulk-enqueue downloads for every discovered video
        // AND every archived-but-outdated video. The latter happens
        // when the user's quality settings (maxResolution /
        // codecPreference) no longer match what was stamped at archive
        // time - the backend will allow those through and the worker
        // will replace the R2 object in place at the new quality.
        const currentQuality = {
          resolution: settings!.maxResolution,
          codec: settings!.codecPreference,
        }
        const unsynced = liveVideos
          .filter(
            (v) =>
              v.status === "discovered" ||
              (v.status === "archived" &&
                isQualityOutdated(currentQuality, {
                  resolution: v.archivedMaxResolution,
                  codec: v.archivedCodecPreference,
                }))
          )
          .map((v) => v.id)
        if (unsynced.length > 0) {
          // Optimistic UI: flip them all to syncing so the bars
          // start animating immediately.
          setVideos((curr) =>
            curr.map((v) =>
              unsynced.includes(v.id)
                ? { ...v, status: "syncing", syncProgress: 0 }
                : v
            )
          )
          try {
            const res = await fetch(
              `/api/youtube/channels/${encodeURIComponent(channelId)}/sync-files`,
              {
                method: "POST",
                credentials: "include",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ video_ids: unsynced }),
              }
            )
            if (!res.ok) {
              setVideos((curr) =>
                curr.map((v) =>
                  unsynced.includes(v.id)
                    ? { ...v, status: "discovered", syncProgress: undefined }
                    : v
                )
              )
              failures.push(`Couldn't queue downloads (HTTP ${res.status}).`)
            }
          } catch {
            failures.push("Couldn't reach the server to queue downloads.")
          }
        }
      }

      if (options.captions) {
        // Captions backfill: enqueue captions-kind jobs for every
        // already-archived video. The worker runs yt-dlp --skip-download
        // --write-subs, uploads each VTT to R2, and reports the
        // languages back via /complete. New videos archived after this
        // run get their captions automatically as a side-effect of
        // the normal video sync (yt-dlp writes subs alongside the mp4),
        // so this is strictly for catching up on tracks the channel
        // owner added after the original archive.
        try {
          const res = await fetch(
            `/api/youtube/channels/${encodeURIComponent(channelId)}/sync-captions`,
            { method: "POST", credentials: "include" }
          )
          if (!res.ok) {
            failures.push(`Captions backfill returned ${res.status}.`)
          }
        } catch {
          failures.push("Couldn't reach the server to queue captions.")
        }
      }

      if (options.metadata) {
        try {
          const res = await fetch(
            `/api/youtube/channels/${encodeURIComponent(channelId)}/sync-metadata`,
            {
              method: "POST",
              credentials: "include",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ fields: options.fields }),
            }
          )
          if (!res.ok) {
            failures.push(
              res.status === 502
                ? "Couldn't reach YouTube to refresh metadata."
                : `Metadata refresh returned ${res.status}.`
            )
          } else {
            await refetchVideosFromServer()
          }
        } catch {
          failures.push("Couldn't reach the server for metadata refresh.")
        }
      }

      if (failures.length > 0) {
        toast({
          title:
            failures.length === 1
              ? "Sync partially completed"
              : `Sync completed with ${failures.length} issues`,
          description: failures.join(" "),
          variant: "error",
        })
      }
    } finally {
      setSyncing(false)
    }
  }

  // The backend flag is named "terminated", but it only means two consecutive
  // scrapes of the channel's /about page came back empty - and every failure
  // mode (timeout, HTTP error, bot interstitial, YouTube changing their
  // markup) collapses into that same signal. "Terminated" is YouTube's word
  // for a punitive ban, so claiming it here would tell users something we
  // cannot establish. The copy states only what we observed.
  const terminated = channel.youtubeStatus === "terminated"

  return (
    <div>
      {terminated && (
        <div className="border-b-2 border-red-500 bg-red-500/10 px-8 py-3">
          <div className="max-w-4xl mx-auto flex items-center gap-3 text-sm">
            <AlertTriangle className="size-5 text-red-400 shrink-0" />
            <div>
              <span className="font-bold text-red-400">
                We can't reach this channel on YouTube.
              </span>{" "}
              <span className="text-muted-foreground">
                Your archive is preserved - nothing on ARCHIVE336's servers has
                been deleted.
              </span>
            </div>
          </div>
        </div>
      )}
      {/* Channel header */}
      <div className="border-b border-border bg-card/30">
        <div className="p-8 max-w-4xl mx-auto">
          {/* Top chrome row: nav + actions */}
          <div className="flex items-center justify-between gap-3 mb-6">
            <Button
              variant="ghost"
              size="sm"
              asChild
              className="-ml-3 text-muted-foreground"
            >
              <Link to="/youtube">
                <ArrowLeft />
                Back to YouTube
              </Link>
            </Button>
            <div className="flex items-center gap-2">
              <Button
                asChild
                variant="outline"
                size="icon"
                aria-label="Open channel on YouTube"
              >
                <a
                  href={channelUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  <ExternalLink />
                </a>
              </Button>
              {/* Both halves matter: the user asked for comment sync AND
                  the channel can actually do it. Comment sync needs the
                  Data API, so a channel authenticated through the worker
                  app never syncs any, and showing the entry point off the
                  toggle alone opens a permanently empty panel. */}
              {settings?.syncComments && channel.commentsSyncAvailable !== false && (
                <>
                  <Button
                    variant="outline"
                    size="icon"
                    aria-label="Recently deleted comments"
                    onClick={() => setDeletedCommentsOpen(true)}
                  >
                    <Trash2 />
                  </Button>
                  <Button
                    asChild
                    variant="outline"
                    size="icon"
                    aria-label="Search archived comments"
                  >
                    <Link
                      to={`/youtube/channel/${encodeURIComponent(channelId)}/comments`}
                    >
                      <MessageSquare />
                    </Link>
                  </Button>
                </>
              )}
              <Button
                variant="outline"
                size="icon"
                aria-label="Channel archive settings"
                onClick={() => setSettingsOpen(true)}
              >
                <Settings />
              </Button>
              {/* Paused channels do not sync, by any route - the server
                  enforces it with a 409. Rather than show a Sync button
                  that refuses, show the switch that would let it work.
                  It replaces the button while paused and gives the space
                  back the moment the channel is on. */}
              {settings?.active === false ? (
                <div className="flex items-center gap-2 border border-border px-3 h-9">
                  <span className="text-sm text-muted-foreground">Paused</span>
                  <Switch
                    checked={false}
                    disabled={activating}
                    onCheckedChange={() => void activateChannel()}
                    aria-label="Activate channel"
                  />
                </div>
              ) : (
                <Button
                  variant="outline"
                  onClick={() => setSyncPanelOpen(true)}
                  disabled={syncing}
                >
                  <RefreshCw
                    className={syncing ? "animate-spin" : undefined}
                  />
                  {syncing ? "Syncing…" : "Sync"}
                </Button>
              )}
              <Button
                variant="outline"
                onClick={() => setDownloadPanelOpen(true)}
              >
                <Download
                  className={downloadBusy ? "animate-download-pulse" : undefined}
                />
                {downloadBusy ? "Downloading" : "Download"}
              </Button>
            </div>
          </div>

          {/* Identity + bio: avatar+name floats left, bio wraps next to it,
              then reclaims full width once it extends past the avatar's bottom */}
          <div className="flow-root">
            <div className="float-left flex items-center gap-4 mr-6 mb-2">
              {(settings ?? channel.settings).saveChannelAvatar && (
                <ChannelAvatar
                  url={channel.avatarUrl}
                  name={channel.name || channel.handle || ""}
                  size="size-20"
                  textClassName="text-2xl"
                />
              )}
              <div className="min-w-0">
                {(settings ?? channel.settings).saveChannelAbout ? (
                  <>
                    <h1 className="text-2xl font-extrabold tracking-tight">
                      {channel.name}
                    </h1>
                    <div className="text-sm text-muted-foreground font-mono mt-0.5">
                      {channel.handle}
                    </div>
                    {(channel.country || channel.joinedAt) && (
                      <div className="text-xs text-muted-foreground font-mono mt-1">
                        {abbreviateCountry(channel.country)}
                        {channel.country && channel.joinedAt ? " · " : ""}
                        {channel.joinedAt
                          ? `Joined ${formatMonthYear(channel.joinedAt)}`
                          : ""}
                      </div>
                    )}
                  </>
                ) : (
                  <h1 className="text-2xl font-extrabold tracking-tight">
                    {channel.handle}
                  </h1>
                )}
              </div>
            </div>

            {(settings ?? channel.settings).saveChannelAbout &&
              channel.description && (
                <p className="text-sm text-neutral-300 leading-relaxed">
                  {channel.description}
                </p>
              )}

            {(settings ?? channel.settings).saveChannelAbout &&
              channel.links.length > 0 && (
                <div className="flex flex-wrap items-center gap-x-5 gap-y-2 mt-4 clear-left">
                  {channel.links.map((link) => (
                    <a
                      key={link.url}
                      href={link.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1.5 text-sm text-neutral-300"
                    >
                      <ExternalLink className="size-3" />
                      <span className="font-semibold">{link.label}</span>
                    </a>
                  ))}
                </div>
              )}
          </div>

          {(settings ?? channel.settings).saveChannelStatsSnapshots && (
            <div className="grid grid-cols-4 gap-4 mt-6">
              <Stat
                icon={<Users className="size-3.5" />}
                label="Subscribers"
                value={formatCount(channel.subscriberCount)}
              />
              <Stat
                icon={<VideoIcon className="size-3.5" />}
                label="Videos"
                value={formatCount(channel.videoCount)}
              />
              <Stat
                icon={<Eye className="size-3.5" />}
                label="Views"
                value={formatCount(channel.totalViews)}
              />
              <Stat
                icon={<Activity className="size-3.5" />}
                label="Latest activity"
                value={formatRelativeDate(latestUploadISO)}
              />
            </div>
          )}

          <div className="h-px bg-border my-4" />

              <div className="grid grid-cols-4 gap-4">
                <Stat
                  icon={<Archive className="size-3.5" />}
                  label="Archived"
                  value={
                    // Progress-style: "X / Y" while syncing, just "X"
                    // when every known video is archived. Denominator
                    // is the count of videos WE have rows for, not
                    // YouTube's reported channel.videoCount - we may
                    // have preserved videos that have since been
                    // removed from YouTube's public count and that's
                    // not a "still syncing" state.
                    savedCount >= videos.length
                      ? String(savedCount)
                      : `${savedCount} / ${videos.length}`
                  }
                />
                <Stat
                  icon={<HardDrive className="size-3.5" />}
                  label="Storage"
                  // The backend's billed-bytes figure (videos + their
                  // thumbnails), not a client-side sum of video files -
                  // so this number times the advertised rate is the Cost
                  // two stats over. Falls back for older payloads.
                  value={formatBytes(channel.bytesStored ?? totalArchivedBytes)}
                />
                <Stat
                  icon={<Clock className="size-3.5" />}
                  label="Duration"
                  value={formatTotalDuration(totalArchivedDuration)}
                />
                <Stat
                  icon={<DollarSign className="size-3.5" />}
                  label="Cost"
                  // Monthly storage cost. Prefers the backend's
                  // authoritative projectedMonthlyCostUsd (computed
                  // against the user's effective markup) when present,
                  // falls back to a client-side estimate for legacy
                  // payloads. Bandwidth costs only apply when the
                  // user actively downloads, so they don't belong in
                  // this constant-baseline number. Anything stored
                  // rounds up to a cent; only an empty channel is $0.00.
                  value={(() => {
                    const cost =
                      channel.projectedMonthlyCostUsd ??
                      estimateMonthlyStorageCostUsd(
                        totalArchivedBytes,
                        prices.storagePerGbMonth,
                      )
                    return formatMonthlyCost(cost)
                  })()}
                />
              </div>
        </div>
      </div>

      {/* Videos */}
      <div className="p-8 max-w-4xl mx-auto">
        <Tabs value={activePresetId} onValueChange={applyPreset}>
          <div className="flex items-center justify-between flex-wrap gap-3">
            <TabsList>
              {(displaySettings?.filterPresets ?? []).map((p) => (
                <TabsTrigger key={p.id} value={p.id}>
                  {p.label}{" "}
                  <span className="ml-1.5 text-muted-foreground font-mono tabular-nums">
                    {presetCounts[p.id] ?? 0}
                  </span>
                </TabsTrigger>
              ))}
            </TabsList>

            <div className="flex items-center gap-2">
              {/* Search */}
              <div className="relative w-56">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 size-4 text-muted-foreground pointer-events-none" />
                <input
                  type="text"
                  placeholder="Search videos"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  className="h-9 w-full border border-border bg-transparent pl-9 pr-8 text-sm text-foreground outline-none placeholder:text-muted-foreground focus:border-white focus:bg-white/5"
                />
                {search && (
                  <button
                    type="button"
                    onClick={() => setSearch("")}
                    aria-label="Clear search"
                    className="absolute right-2 top-1/2 -translate-y-1/2 size-5 flex items-center justify-center text-muted-foreground cursor-pointer"
                  >
                    <X className="size-3.5" />
                  </button>
                )}
              </div>

              {/* Sort dimension */}
              <Select
                value={sortDimension}
                onValueChange={(v) => setSortDimension(v as SortDimension)}
              >
                <SelectTrigger className="w-auto">
                  <SelectValue>
                    {SORT_DIMENSION_LABELS[sortDimension]}
                  </SelectValue>
                </SelectTrigger>
                <SelectContent>
                  {(Object.keys(SORT_DIMENSION_LABELS) as SortDimension[]).map(
                    (k) => (
                      <SelectItem key={k} value={k}>
                        {SORT_DIMENSION_LABELS[k]}
                      </SelectItem>
                    )
                  )}
                </SelectContent>
              </Select>

              {/* Sort direction */}
              <Button
                variant="outline"
                size="icon"
                onClick={() =>
                  setSortDirection((d) => (d === "asc" ? "desc" : "asc"))
                }
                aria-label={
                  sortDirection === "asc"
                    ? "Ascending — click to flip"
                    : "Descending — click to flip"
                }
              >
                {sortDirection === "asc" ? <ArrowUp /> : <ArrowDown />}
              </Button>

              {/* View mode */}
              <div className="flex">
                <button
                  type="button"
                  onClick={() => setViewMode("grid")}
                  aria-label="Grid view"
                  aria-pressed={viewMode === "grid"}
                  className={cn(
                    "size-9 flex items-center justify-center border cursor-pointer",
                    viewMode === "grid"
                      ? "bg-white text-black border-white"
                      : "border-border text-muted-foreground"
                  )}
                >
                  <LayoutGrid className="size-4" />
                </button>
                <button
                  type="button"
                  onClick={() => setViewMode("list")}
                  aria-label="List view"
                  aria-pressed={viewMode === "list"}
                  className={cn(
                    "size-9 flex items-center justify-center border border-l-0 cursor-pointer",
                    viewMode === "list"
                      ? "bg-white text-black border-white"
                      : "border-border text-muted-foreground"
                  )}
                >
                  <LayoutList className="size-4" />
                </button>
              </div>

              {/* Filter */}
              <Popover>
                <PopoverTrigger asChild>
                  <Button variant="outline">
                    <SlidersHorizontal />
                    Filter
                  </Button>
                </PopoverTrigger>
                <PopoverContent className="w-80">
                  <div className="space-y-5">
                    <div>
                      <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-2">
                        Upload date
                      </div>
                      <div className="grid grid-cols-2 gap-2">
                        <div>
                          <label className="text-[10px] text-muted-foreground block mb-1">
                            From
                          </label>
                          <input
                            type="date"
                            value={dateFrom}
                            onChange={(e) => setDateFrom(e.target.value)}
                            className="h-9 w-full border border-border bg-transparent px-2 text-sm text-foreground outline-none focus:border-white [color-scheme:dark]"
                          />
                        </div>
                        <div>
                          <label className="text-[10px] text-muted-foreground block mb-1">
                            To
                          </label>
                          <input
                            type="date"
                            value={dateTo}
                            onChange={(e) => setDateTo(e.target.value)}
                            className="h-9 w-full border border-border bg-transparent px-2 text-sm text-foreground outline-none focus:border-white [color-scheme:dark]"
                          />
                        </div>
                      </div>
                    </div>

                    <div>
                      <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-2">
                        Visibility
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        {VISIBILITY_OPTIONS.map((v) => {
                          const active = visibilityFilter.has(v)
                          return (
                            <button
                              key={v}
                              type="button"
                              onClick={() => toggleVisibility(v)}
                              className={cn(
                                "px-3 py-1 text-xs font-semibold border cursor-pointer",
                                active
                                  ? "bg-white text-black border-white"
                                  : "border-border text-foreground"
                              )}
                            >
                              {VISIBILITY_LABELS[v]}
                            </button>
                          )
                        })}
                      </div>
                    </div>

                    <div>
                      <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-2">
                        Type
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        {TYPE_OPTIONS.map((o) => {
                          const active = typeFilter.has(o.value)
                          return (
                            <button
                              key={o.value}
                              type="button"
                              onClick={() => toggleType(o.value)}
                              className={cn(
                                "px-3 py-1 text-xs font-semibold border cursor-pointer",
                                active
                                  ? "bg-white text-black border-white"
                                  : "border-border text-foreground"
                              )}
                            >
                              {o.label}
                            </button>
                          )
                        })}
                      </div>
                    </div>

                    {activeFilterCount > 0 && (
                      <div className="pt-3 border-t border-border">
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
                </PopoverContent>
              </Popover>
            </div>
          </div>

          {(displaySettings?.filterPresets ?? []).map((p) => (
            <TabsContent key={p.id} value={p.id} className="mt-6">
              {processed.length === 0 && videosLoading ? (
                /* Nothing to say yet. The list is empty during the fetch
                   for the same reason it is empty before a scan, so
                   answering that question mid-load flashed "Waiting for
                   the first scan" on every refresh of a fully archived
                   channel - the one message guaranteed to alarm someone
                   whose backup is fine. */
                <p className="text-sm text-muted-foreground">Loading videos…</p>
              ) : processed.length === 0 ? (
                <EmptyState {...emptyStateFor(p.label)} />
              ) : (
                <>
                  {viewMode === "grid" ? (
                    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-6">
                      {pageVideos.map((v) => (
                        <VideoCard
                          key={v.id}
                          video={v}
                          onClick={openVideo}
                          settings={displaySettings ?? channel.settings}
                          selected={selectedVideoIds.has(v.id)}
                          onToggleSelect={toggleSelect}
                          variant="grid"
                          workerActive={workerStatus.active}
                        />
                      ))}
                    </div>
                  ) : (
                    <div className="space-y-1.5">
                      {pageVideos.map((v) => (
                        <VideoCard
                          key={v.id}
                          video={v}
                          onClick={openVideo}
                          settings={displaySettings ?? channel.settings}
                          selected={selectedVideoIds.has(v.id)}
                          onToggleSelect={toggleSelect}
                          variant="list"
                          workerActive={workerStatus.active}
                        />
                      ))}
                    </div>
                  )}

                  {totalPages > 1 && (
                    <div className="flex items-center justify-center gap-2 mt-8">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() =>
                          setCurrentPage((p) => Math.max(1, p - 1))
                        }
                        disabled={safePage === 1}
                      >
                        <ChevronLeft />
                        Previous
                      </Button>
                      <div className="text-sm text-muted-foreground font-mono tabular-nums px-3">
                        Page{" "}
                        <span className="text-foreground font-semibold">
                          {safePage}
                        </span>{" "}
                        of{" "}
                        <span className="text-foreground font-semibold">
                          {totalPages}
                        </span>
                      </div>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() =>
                          setCurrentPage((p) => Math.min(totalPages, p + 1))
                        }
                        disabled={safePage === totalPages}
                      >
                        Next
                        <ChevronRight />
                      </Button>
                    </div>
                  )}

                  {/* Background-load indicator. The videos endpoint is
                      cursor-paginated; the initial mount effect chains
                      pages back-to-back so we show this hint while
                      additional pages stream in. Disappears as soon as
                      the cursor walk completes. */}
                  {videosLoading && videos.length > 0 && (
                    <div className="mt-4 text-center text-xs text-muted-foreground font-mono tabular-nums">
                      Loading more videos ({videos.length.toLocaleString()} so far)…
                    </div>
                  )}
                </>
              )}
            </TabsContent>
          ))}
        </Tabs>
      </div>

      {settings && (
        <>
          <SyncPanel
            channel={channel}
            settings={settings}
            allVideos={videos}
            open={syncPanelOpen}
            onOpenChange={setSyncPanelOpen}
            onConfirm={(options) => {
              void runSync(options)
            }}
          />

          <DownloadPanel
            channel={channel}
            videos={videos}
            selectedVideos={videos.filter((v) => selectedVideoIds.has(v.id))}
            initialFilters={{
              search,
              dateFrom,
              dateTo,
              visibilities: Array.from(visibilityFilter),
              types: Array.from(typeFilter),
            }}
            settings={settings}
            open={downloadPanelOpen}
            onOpenChange={setDownloadPanelOpen}
            onBusyChange={setDownloadBusy}
          />

          <VideoDetailPanel
            video={detailVideo}
            open={!!detailVideo}
            onOpenChange={(o) => !o && setDetailVideo(null)}
            settings={settings}
            commentsAvailable={channel.commentsSyncAvailable !== false}
            onSync={(v) => {
              handleSync(v)
              setDetailVideo(null)
            }}
            onRetry={(v) => {
              handleRetry(v)
              setDetailVideo(null)
            }}
          />

          <ChannelSettingsPanel
            channel={channel}
            settings={settings}
            globalSettings={globalSettings ?? settings}
            onSaveGlobal={saveGlobalSettings}
            open={settingsOpen}
            onOpenChange={setSettingsOpen}
            estimatedChannelDurationSec={
              videos.length > 0
                ? (videos.reduce((acc, v) => acc + v.durationSec, 0) /
                    videos.length) *
                  channel.videoCount
                : 600 * channel.videoCount
            }
            // The same actual-archived figures the header shows, so the
            // panel's "if fully synced" estimate is anchored to the user's
            // real bytes-per-hour instead of a generic bitrate guess.
            archivedBytes={totalArchivedBytes}
            archivedDurationSec={totalArchivedDuration}
            onReset={async () => {
              const res = await fetch(
                `/api/youtube/channels/${encodeURIComponent(channelId)}/settings/reset`,
                { method: "POST", credentials: "include" }
              ).catch(() => null)
              if (!res || !res.ok) {
                toast({
                  title: "Couldn't reset settings",
                  description: res
                    ? `Server returned ${res.status}.`
                    : "Backend unreachable.",
                  variant: "error",
                })
                throw new Error("reset failed")
              }
              const body = (await res.json()) as {
                settings: ChannelArchiveSettings
              }
              // Re-read the channel from the server rather than trusting
              // the response plus local propagation. The write is confirmed
              // stored at this point, so the only remaining question is
              // whether every piece of page state agrees - and this page
              // holds settings in more than one place. Threading the new
              // object through each of them by hand is how one of them ends
              // up stale and the panel shows a value the database does not
              // have, which is precisely the confusion this whole feature
              // exists to end.
              const fresh = await fetch(
                `/api/youtube/channels/${encodeURIComponent(channelId)}`,
                { credentials: "include" }
              )
                .then((r) => (r.ok ? r.json() : null))
                .catch(() => null)
              const applied = normalizeChannelSettings(
                (fresh?.settings as ChannelArchiveSettings | undefined) ??
                  body.settings
              )
              setSettings(applied)
              setChannel((c) =>
                fresh ? { ...fresh, settings: applied } : c
                  ? { ...c, settings: applied }
                  : c
              )
              toast({ title: "Settings reset to your defaults" })
              return applied
            }}
            onSave={async (next) => {
              const res = await fetch(
                `/api/youtube/channels/${encodeURIComponent(channelId)}`,
                {
                  method: "PUT",
                  credentials: "include",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ ...channel, settings: next }),
                }
              ).catch(() => null)
              if (!res) {
                toast({
                  title: "Couldn't save settings",
                  description: "Backend unreachable — try again.",
                  variant: "error",
                })
                throw new Error("Backend unreachable")
              }
              if (res.status === 502) {
                toast({
                  title: "Couldn't reach YouTube",
                  description: "Couldn't fetch the channel's profile picture. Try again in a moment.",
                  variant: "error",
                })
                throw new Error("YouTube unreachable")
              }
              if (!res.ok) {
                toast({
                  title: "Couldn't save settings",
                  description: `Server returned ${res.status}.`,
                  variant: "error",
                })
                throw new Error(`HTTP ${res.status}`)
              }
              const updated = (await res.json()) as Channel
              setSettings(next)
              setChannel(updated)
              // Settings save can trigger video discovery on the backend
              // (e.g. when "Automatically sync" goes from off - on). Re-fetch
              // so any newly discovered videos show up immediately. The
              // shared refetchVideos helper walks all cursor pages and
              // bulk-presigns thumbnails per page.
              void refetchVideos()
            }}
            onRemove={async () => {
              const res = await fetch(
                `/api/youtube/channels/${encodeURIComponent(channelId)}`,
                {
                  method: "DELETE",
                  credentials: "include",
                }
              ).catch(() => null)
              if (!res || (!res.ok && res.status !== 404)) {
                toast({
                  title: "Couldn't remove channel",
                  description: "Backend unreachable — try again.",
                  variant: "error",
                })
                throw new Error("delete failed")
              }
              navigate("/youtube")
            }}
          />

          <DeletedCommentsSheet
            channelId={channelId}
            open={deletedCommentsOpen}
            onOpenChange={setDeletedCommentsOpen}
            videoLookup={(vid) => {
              const v = videos.find((x) => x.id === vid)
              return v ? { title: v.title } : null
            }}
          />
        </>
      )}
    </div>
  )
}

function Stat({
  icon,
  label,
  value,
  highlight,
}: {
  icon?: React.ReactNode
  label: string
  value: string
  highlight?: boolean
}) {
  return (
    <div className="min-w-0 text-center">
      <div
        className={cn(
          "flex items-center justify-center gap-1.5 text-[10px] uppercase tracking-wider",
          highlight ? "text-amber-400" : "text-muted-foreground"
        )}
      >
        {icon}
        <span className="truncate">{label}</span>
      </div>
      <div
        className={cn(
          "mt-1 text-sm font-bold font-mono tabular-nums truncate",
          highlight ? "text-amber-400" : "text-foreground"
        )}
      >
        {value}
      </div>
    </div>
  )
}
