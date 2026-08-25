import * as React from "react"
import JSZip from "jszip"
import { Download } from "lucide-react"
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetBody,
  SheetFooter,
} from "./ui/sheet"
import { Button } from "./ui/button"
import { Progress } from "./ui/progress"
import { Switch } from "./ui/switch"
import { useToast } from "./ui/toast"
import { cn } from "@/lib/utils"
import { estimateDownloadCostUsd, estimateGbPerHour } from "@/lib/estimates"
import { usePrices } from "@/lib/pricing"
import { formatBytes, formatGb, formatUsd } from "@/lib/format"
import { triggerBlobDownload } from "@/lib/download"
// One shared list + label map, not a hand-copied one per surface. Three
// copies of the same options is how the filter chips drift apart.
import { VISIBILITY_OPTIONS, VISIBILITY_LABELS } from "./PresetEditor"
import type {
  Channel,
  ChannelArchiveSettings,
  Video,
  VideoType,
  VideoVisibility,
} from "@/lib/types"

export type MetadataSelection = {
  saveThumbnail: boolean
  saveViewCount: boolean
  saveDescription: boolean
  saveTags: boolean
  /** Bundle manual caption tracks (.vtt files) into the per-video
   *  subfolder of the bulk download. captionLanguages always appears
   *  in metadata.json regardless of this toggle. */
  saveCaptions: boolean
}

export type DownloadFilters = {
  search: string
  dateFrom: string
  dateTo: string
  visibilities: VideoVisibility[]
  types: VideoType[]
}

type DownloadScope = "selected" | "current" | "all"

type Props = {
  channel: Channel
  videos: Video[]
  selectedVideos: Video[]
  initialFilters: DownloadFilters
  settings: ChannelArchiveSettings
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Optional notification when the user kicks off a bulk download.
   *  The actual download work is handled inside this panel - this
   *  prop is just a hook for the parent to (eg) record telemetry
   *  or update its own UI. Safe to omit. */
  onStart?: (videos: Video[], metadata: MetadataSelection) => void
  /** Fires whenever a bulk download starts or finishes. Lets the
   *  parent surface a busy state on its own UI (eg. animating the
   *  Download button on the channel page) even when the sheet
   *  itself is closed. Safe to omit. */
  onBusyChange?: (busy: boolean) => void
}

const TYPE_OPTIONS: { value: VideoType; label: string }[] = [
  { value: "video", label: "Video" },
  { value: "short", label: "Short" },
  { value: "livestream", label: "Livestream" },
]

const EMPTY_FILTERS: DownloadFilters = {
  search: "",
  dateFrom: "",
  dateTo: "",
  visibilities: [],
  types: [],
}

function videoVisibility(v: Video): VideoVisibility {
  if (v.status === "deleted_on_youtube") return "deleted"
  // One backend writer still stores members-only as "members_only", which
  // matches no chip: the card shows a Members badge and then the row vanishes
  // the moment you filter by Members. Cast because the stray spelling sits
  // outside the declared union (VideoCard tolerates it the same way).
  if ((v.privacy as string) === "members_only") return "members"
  return v.privacy
}

function metadataFromSettings(s: ChannelArchiveSettings): MetadataSelection {
  return {
    saveThumbnail: s.saveThumbnail,
    saveViewCount: s.saveViewCount,
    saveDescription: s.saveDescription,
    saveTags: s.saveTags,
    // Default-on - captions are already archived, including them in
    // the bulk-download bundle costs nothing extra and matches the
    // per-video download panel's default.
    saveCaptions: true,
  }
}

export function DownloadPanel({
  channel,
  videos,
  selectedVideos,
  initialFilters,
  settings,
  open,
  onOpenChange,
  onStart,
  onBusyChange,
}: Props) {
  const [scope, setScope] = React.useState<DownloadScope>(
    selectedVideos.length > 0 ? "selected" : "current"
  )
  const [search, setSearch] = React.useState(initialFilters.search)
  const [dateFrom, setDateFrom] = React.useState(initialFilters.dateFrom)
  const [dateTo, setDateTo] = React.useState(initialFilters.dateTo)
  const [visibilities, setVisibilities] = React.useState<Set<VideoVisibility>>(
    new Set(initialFilters.visibilities)
  )
  const [types, setTypes] = React.useState<Set<VideoType>>(
    new Set(initialFilters.types)
  )
  const [metadata, setMetadata] = React.useState<MetadataSelection>(
    metadataFromSettings(settings)
  )

  // Reset state every time the panel opens. Default to Selected scope when
  // the user already picked videos; otherwise fall back to Current view.
  React.useEffect(() => {
    if (!open) return
    setScope(selectedVideos.length > 0 ? "selected" : "current")
    setSearch(initialFilters.search)
    setDateFrom(initialFilters.dateFrom)
    setDateTo(initialFilters.dateTo)
    setVisibilities(new Set(initialFilters.visibilities))
    setTypes(new Set(initialFilters.types))
    setMetadata(metadataFromSettings(settings))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const applyFilters = (f: DownloadFilters) => {
    setSearch(f.search)
    setDateFrom(f.dateFrom)
    setDateTo(f.dateTo)
    setVisibilities(new Set(f.visibilities))
    setTypes(new Set(f.types))
  }

  const handleScopeChange = (next: DownloadScope) => {
    setScope(next)
    if (next === "current") {
      applyFilters(initialFilters)
    } else {
      // "selected" and "all" don't use the filter editor
      applyFilters(EMPTY_FILTERS)
    }
  }

  const toggleVisibility = (v: VideoVisibility) => {
    setVisibilities((prev) => {
      const next = new Set(prev)
      if (next.has(v)) next.delete(v)
      else next.add(v)
      return next
    })
  }

  const toggleType = (t: VideoType) => {
    setTypes((prev) => {
      const next = new Set(prev)
      if (next.has(t)) next.delete(t)
      else next.add(t)
      return next
    })
  }

  const matchingVideos = React.useMemo(() => {
    if (scope === "selected") return selectedVideos
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
        result = result.filter(
          (v) => new Date(v.uploadDate).getTime() >= from
        )
      }
    }
    if (dateTo) {
      const to = new Date(dateTo).getTime() + 86_400_000
      if (!isNaN(to)) {
        result = result.filter((v) => new Date(v.uploadDate).getTime() < to)
      }
    }
    if (visibilities.size > 0) {
      result = result.filter((v) => visibilities.has(videoVisibility(v)))
    }
    if (types.size > 0) {
      result = result.filter((v) => types.has(v.type))
    }
    return result
  }, [scope, selectedVideos, videos, search, dateFrom, dateTo, visibilities, types])

  const totalDurationSec = matchingVideos.reduce(
    (sum, v) => sum + v.durationSec,
    0
  )
  const prices = usePrices()
  const gbPerHour = estimateGbPerHour(
    settings.maxResolution,
    settings.codecPreference
  )
  const estimatedGb = (gbPerHour * totalDurationSec) / 3600
  const estimatedCostUsd = estimateDownloadCostUsd(
    estimatedGb,
    prices.downloadPerGb,
  )

  // ---- Bulk download runner ----
  //
  // Iterates the selected videos sequentially. For each, fetches
  // /download-parts to get presigned R2 URLs + the metadata blob,
  // then pulls the mp4 / thumbnail / captions / metadata into a
  // per-video subfolder of one JSZip. Progress is 0-100 across the
  // whole bundle; the AbortController lets the Cancel button stop
  // the run cleanly even mid-mp4.
  //
  // Sequential rather than parallel because (a) browsers cap
  // simultaneous origin connections at ~6, (b) running multiple big
  // mp4 fetches at once on a residential uplink doesn't actually
  // get them done faster, and (c) sequential progress is much easier
  // to surface in the UI ('Video 3 of 11' vs juggling N concurrent
  // bars). If we ever want to speed it up, the thumbnail + caption
  // fetches inside one video step could be parallelized first since
  // they're tiny.
  const { toast } = useToast()
  const [busy, setBusy] = React.useState(false)
  const [progress, setProgress] = React.useState(0)
  const [statusLine, setStatusLine] = React.useState("")
  // Phase decides whether we surface speed / ETA (only meaningful
  // during the network-bound 'downloading' pass; the local-CPU
  // 'packaging' pass would compute misleadingly low speeds).
  const [phase, setPhase] = React.useState<"downloading" | "packaging">(
    "downloading"
  )
  // Bytes tracking - drives MB / speed / ETA. Raw counters live in
  // refs so we can poke them at fetch-stream chunk granularity
  // without forcing a React re-render on every chunk; the tick below
  // flushes the snapshot into state at a sane 4 Hz cadence.
  const [bytesDownloaded, setBytesDownloaded] = React.useState(0)
  const [bytesTotal, setBytesTotal] = React.useState(0)
  const [speedBps, setSpeedBps] = React.useState(0)
  const [videosCompleted, setVideosCompleted] = React.useState(0)
  const [currentVideo, setCurrentVideo] = React.useState<Video | null>(null)

  const abortRef = React.useRef<AbortController | null>(null)
  const bytesRef = React.useRef(0)
  const samplesRef = React.useRef<{ ts: number; bytes: number }[]>([])

  const cancelBulk = () => {
    if (abortRef.current) abortRef.current.abort()
  }

  // Bubble busy state up to the parent (eg. ChannelDetail) so it can
  // surface its own indicator on the Download-trigger button even
  // when this sheet is closed. Effect-on-change beats threading the
  // callback through every setBusy call site.
  React.useEffect(() => {
    onBusyChange?.(busy)
  }, [busy, onBusyChange])

  // Sampling tick: while busy, every 250ms read bytesRef + a rolling
  // 3-second window of samples to compute speed and ETA. Effect
  // cleans itself up when busy flips back off so we don't leak the
  // interval after the run finishes.
  React.useEffect(() => {
    if (!busy) return
    const tick = () => {
      const now = Date.now()
      const samples = samplesRef.current
      samples.push({ ts: now, bytes: bytesRef.current })
      while (samples.length > 1 && now - samples[0].ts > 3000) {
        samples.shift()
      }
      let speed = 0
      if (samples.length >= 2) {
        const a = samples[0]
        const b = samples[samples.length - 1]
        const dt = (b.ts - a.ts) / 1000
        if (dt > 0.25) speed = Math.max(0, (b.bytes - a.bytes) / dt)
      }
      setBytesDownloaded(bytesRef.current)
      setSpeedBps(speed)
    }
    tick()
    const id = window.setInterval(tick, 250)
    return () => window.clearInterval(id)
  }, [busy, bytesTotal, phase])

  // Streamed fetch: drains the Response body chunk-by-chunk so we
  // can bump bytesRef as each chunk lands. Falls back to .blob()
  // when the body isn't a readable stream (older Safari). Throws on
  // non-2xx so the caller's try/catch flow stays consistent.
  const fetchBytes = async (
    url: string,
    signal: AbortSignal
  ): Promise<Blob | null> => {
    const res = await fetch(url, { signal })
    if (!res.ok) return null
    if (!res.body) return res.blob()
    const reader = res.body.getReader()
    const chunks: Uint8Array[] = []
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      if (value) {
        chunks.push(value)
        bytesRef.current += value.length
      }
    }
    return new Blob(chunks as BlobPart[])
  }

  const runBulkDownload = async () => {
    if (matchingVideos.length === 0) return
    if (onStart) onStart(matchingVideos, metadata)

    // Pre-compute the total mp4 bytes. fileSizeBytes only exists for
    // archived videos; any missing entries contribute 0, which means
    // the speed/ETA stays meaningful for the rest. Thumbnails +
    // captions + metadata.json are kilobytes - ignored in the
    // estimate, the rounding washes them out.
    const totalBytesEst = matchingVideos.reduce(
      (sum, v) => sum + (v.fileSizeBytes ?? 0),
      0
    )

    setBusy(true)
    setProgress(0)
    setStatusLine("")
    setPhase("downloading")
    setBytesTotal(totalBytesEst)
    setBytesDownloaded(0)
    setSpeedBps(0)
    setVideosCompleted(0)
    setCurrentVideo(null)
    bytesRef.current = 0
    samplesRef.current = []
    const ctrl = new AbortController()
    abortRef.current = ctrl

    type Parts = {
      title: string
      safeTitle: string
      video: { url: string | null; filename: string; available: boolean }
      thumbnail: { url: string | null; filename: string; available: boolean }
      metadata: {
        filename: string
        available: boolean
        data: Record<string, unknown>
      }
      captions: { language: string; url: string; filename: string }[]
    }

    try {
      const zip = new JSZip()
      const total = matchingVideos.length

      for (let i = 0; i < total; i++) {
        if (ctrl.signal.aborted) throw new DOMException("aborted", "AbortError")
        const v = matchingVideos[i]
        setCurrentVideo(v)
        setStatusLine(v.title)

        // Per-video sub-progress: 60% of the slot is the mp4, 40% is
        // thumb + captions + metadata + parts-fetch overhead. Keeps the
        // bar moving smoothly rather than long mp4-stalls.
        const slotStart = (i / total) * 100
        const slot = 100 / total
        const setSlot = (frac: number) => setProgress(slotStart + slot * frac)

        // 1) Fetch the manifest of presigned URLs for this video.
        const partsRes = await fetch(
          `/api/youtube/videos/${encodeURIComponent(v.id)}/download-parts`,
          { credentials: "include", signal: ctrl.signal }
        )
        if (!partsRes.ok) {
          // Skip silently and move on - a bulk run shouldn't blow up
          // because one video is in a weird state.
          setSlot(1)
          setVideosCompleted(i + 1)
          continue
        }
        const parts = (await partsRes.json()) as Parts
        setSlot(0.05)

        // Per-video folder named after the safe title. JSZip auto-
        // creates the folder when we use the path-style file() call.
        const folderPath = parts.safeTitle

        // 2) Video file (always included if available). Streamed
        // fetch bumps bytesRef as chunks arrive so the live byte /
        // speed / ETA display stays responsive even mid-mp4.
        if (parts.video.available && parts.video.url) {
          const blob = await fetchBytes(parts.video.url, ctrl.signal)
          if (blob) {
            zip.file(`${folderPath}/${parts.video.filename}`, blob)
          }
        }
        setSlot(0.65)

        // 3) Thumbnail (toggle-gated). Tiny enough that the byte
        // counter doesn't really need it, but stream anyway for
        // consistency with the mp4 path.
        if (
          metadata.saveThumbnail &&
          parts.thumbnail.available &&
          parts.thumbnail.url
        ) {
          const blob = await fetchBytes(parts.thumbnail.url, ctrl.signal)
          if (blob) {
            zip.file(`${folderPath}/${parts.thumbnail.filename}`, blob)
          }
        }
        setSlot(0.75)

        // 4) Captions (toggle-gated). Each VTT in captions/<lang>.vtt.
        if (metadata.saveCaptions && parts.captions.length > 0) {
          for (const cap of parts.captions) {
            if (ctrl.signal.aborted) throw new DOMException("aborted", "AbortError")
            const blob = await fetchBytes(cap.url, ctrl.signal)
            if (blob) {
              zip.file(`${folderPath}/captions/${cap.filename}`, blob)
            }
          }
        }
        setSlot(0.9)

        // 5) Metadata JSON, filtered by per-field toggles. Identity +
        // archive context always included; toggle-gated fields drop
        // out when their switch is off.
        const m = parts.metadata.data
        const filtered: Record<string, unknown> = {
          id: m.id,
          title: m.title,
          uploadDate: m.uploadDate,
          durationSec: m.durationSec,
          privacy: m.privacy,
          type: m.type,
          captionLanguages: m.captionLanguages,
          youtubeUrl: m.youtubeUrl,
          channel: m.channel,
          archivedAt: m.archivedAt,
          firstSeenAt: m.firstSeenAt,
        }
        if (metadata.saveViewCount) filtered.viewCount = m.viewCount
        if (metadata.saveDescription) filtered.description = m.description
        if (metadata.saveTags) filtered.tags = m.tags
        if (metadata.saveThumbnail) {
          filtered.thumbnailUrlOriginal = m.thumbnailUrlOriginal
        }
        zip.file(
          `${folderPath}/${parts.metadata.filename}`,
          JSON.stringify(filtered, null, 2)
        )
        setSlot(1)
        setVideosCompleted(i + 1)
      }

      if (ctrl.signal.aborted) throw new DOMException("aborted", "AbortError")

      // Zip-build pass. The per-video loop ended at 100%, so reset
      // to 0 and let JSZip's onUpdate callback animate the bar back
      // up while it compresses + streams entries to the output
      // blob. For large bundles (multi-GB) this phase can take a
      // long time; without the live progress the user would see a
      // frozen 100% and wonder if it's hung.
      setPhase("packaging")
      setStatusLine("Packaging archive…")
      setProgress(0)
      const zipBlob = await zip.generateAsync(
        { type: "blob" },
        (meta) => {
          if (ctrl.signal.aborted) return
          setProgress(meta.percent)
        }
      )

      // JSZip.generateAsync doesn't honor AbortSignal natively - if
      // the user clicked Cancel partway through the packaging phase,
      // JSZip keeps grinding to completion. So we re-check the abort
      // flag here and bail out BEFORE triggering the actual file
      // save, otherwise we'd hand them a zip they explicitly
      // cancelled.
      if (ctrl.signal.aborted) throw new DOMException("aborted", "AbortError")

      const channelSafe = (channel.name || channel.handle || "channel")
        .replace(/[/\\:*?"<>|\0]/g, "_")
        .trim() || "channel"
      triggerBlobDownload(zipBlob, `${channelSafe}.zip`)
      onOpenChange(false)
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") {
        // Cancelled by the user. No toast - the disappearing progress
        // bar is the confirmation.
      } else {
        toast({
          title: "Download failed",
          description:
            e instanceof Error ? e.message : "Try again in a moment.",
          variant: "error",
        })
      }
    } finally {
      setBusy(false)
      setProgress(0)
      setStatusLine("")
      abortRef.current = null
    }
  }

  // Sheet open/close is a pure UI concern - it never interrupts an
  // in-flight bulk download. The component (DownloadPanel) is always
  // mounted by the parent regardless of the sheet's visibility, so
  // the running async function + its progress/busy state survive
  // closes. Reopening while busy drops the user straight back into
  // the progress view. Only the explicit Cancel button (or a true
  // page unload) aborts the run.

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent>
        <SheetHeader>
          <SheetTitle>Download from {channel.name}</SheetTitle>
        </SheetHeader>

        {busy ? (
          <>
            <SheetBody className="space-y-6">
              <div className="space-y-5">
                <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold">
                  {phase === "packaging" ? "Packaging" : "Downloading"}
                </div>

                {/* Current-video row: thumbnail + position + title.
                    Falls back to a status-line-only layout during
                    the packaging phase (no current video then). */}
                {phase === "downloading" ? (
                  <div className="flex items-start gap-3">
                    {currentVideo?.thumbnailUrl ? (
                      <img
                        draggable={false}
                        src={currentVideo.thumbnailUrl}
                        alt=""
                        className="w-24 h-14 object-cover bg-neutral-900 shrink-0"
                      />
                    ) : (
                      <div className="w-24 h-14 bg-neutral-900 shrink-0" />
                    )}
                    <div className="min-w-0 flex-1">
                      <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono tabular-nums mb-1">
                        Video {Math.min(videosCompleted + 1, matchingVideos.length)} of {matchingVideos.length}
                      </div>
                      <div className="text-sm font-semibold leading-snug line-clamp-2 break-words">
                        {statusLine || "Preparing…"}
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="text-sm font-semibold leading-snug">
                    {statusLine || "Packaging archive…"}
                  </div>
                )}

                {/* Progress takes a 0-1 value and clamps at 1, so we
                    divide our 0-100 percent by 100 before passing it
                    in. h-2 is a touch taller than the default h-1.5
                    so the bar reads more clearly at a glance. */}
                <Progress value={progress / 100} className="h-2" />

                {/* Stats: tiered hierarchy. Percent is the headline
                    (large, bold, white). Bytes-downloaded is the
                    middle tier (normal, foreground). Speed + ETA are
                    the demoted detail row (muted). Elapsed dropped
                    per the user's request - the rest already tells
                    the same story. The packaging pass shows only the
                    percent line since the rest is meaningless once
                    bytes are in memory. */}
                <div className="space-y-1">
                  <div className="text-2xl font-bold font-mono tabular-nums leading-none">
                    {Math.floor(progress)}%
                  </div>
                  {phase === "downloading" && bytesTotal > 0 && (
                    <div className="text-sm font-mono tabular-nums">
                      {formatBytes(bytesDownloaded)} / {formatBytes(bytesTotal)}
                    </div>
                  )}
                  {phase === "downloading" && speedBps > 0 && (
                    <div className="text-xs text-muted-foreground font-mono tabular-nums">
                      {formatBytes(speedBps)}/s
                    </div>
                  )}
                </div>
              </div>
            </SheetBody>
            <SheetFooter>
              <Button variant="destructive" onClick={cancelBulk}>
                Cancel
              </Button>
            </SheetFooter>
          </>
        ) : (
        <>
        <SheetBody className="space-y-6">
          <Section label="Scope">
            <div
              className={cn(
                "grid",
                selectedVideos.length > 0 ? "grid-cols-3" : "grid-cols-2"
              )}
            >
              {selectedVideos.length > 0 && (
                <ScopeButton
                  active={scope === "selected"}
                  onClick={() => handleScopeChange("selected")}
                >
                  Selected ({selectedVideos.length})
                </ScopeButton>
              )}
              <ScopeButton
                active={scope === "current"}
                onClick={() => handleScopeChange("current")}
                isRight={selectedVideos.length > 0}
              >
                Current view
              </ScopeButton>
              <ScopeButton
                active={scope === "all"}
                onClick={() => handleScopeChange("all")}
                isRight
              >
                Entire channel
              </ScopeButton>
            </div>
          </Section>

          {/* Filter inputs only render when scope is 'all' (Entire
              channel). For 'current', the panel just inherits the
              filters already applied in the grid view - the user
              already picked them there, no need to re-show in this
              sheet. For 'selected', the user explicitly chose
              individual videos so filters don't apply at all. */}
          {scope === "all" && (
            <>
              <Section label="Search">
                <input
                  type="text"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="(none)"
                  className="h-9 w-full border border-border bg-transparent px-3 text-sm text-foreground outline-none focus:border-white focus:bg-white/5 placeholder:text-muted-foreground"
                />
              </Section>

              <Section label="Visibility">
                <div className="flex flex-wrap gap-1.5">
                  {VISIBILITY_OPTIONS.map((v) => {
                    const active = visibilities.has(v)
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
              </Section>

              <Section label="Type">
                <div className="flex flex-wrap gap-1.5">
                  {TYPE_OPTIONS.map((o) => {
                    const active = types.has(o.value)
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
              </Section>

              <Section label="Upload date">
                <div className="grid grid-cols-2 gap-2">
                  <input
                    type="date"
                    value={dateFrom}
                    onChange={(e) => setDateFrom(e.target.value)}
                    className="h-9 w-full border border-border bg-transparent px-2 text-sm text-foreground outline-none focus:border-white [color-scheme:dark]"
                  />
                  <input
                    type="date"
                    value={dateTo}
                    onChange={(e) => setDateTo(e.target.value)}
                    className="h-9 w-full border border-border bg-transparent px-2 text-sm text-foreground outline-none focus:border-white [color-scheme:dark]"
                  />
                </div>
              </Section>
            </>
          )}

          <div className="h-px w-full bg-white/60" />

          <Section label="Metadata">
            <ToggleRow
              label="Thumbnail"
              checked={metadata.saveThumbnail}
              onChange={(v) =>
                setMetadata((m) => ({ ...m, saveThumbnail: v }))
              }
            />
            <ToggleRow
              label="View count"
              checked={metadata.saveViewCount}
              onChange={(v) =>
                setMetadata((m) => ({ ...m, saveViewCount: v }))
              }
            />
            <ToggleRow
              label="Description"
              checked={metadata.saveDescription}
              onChange={(v) =>
                setMetadata((m) => ({ ...m, saveDescription: v }))
              }
            />
            <ToggleRow
              label="Tags"
              checked={metadata.saveTags}
              onChange={(v) => setMetadata((m) => ({ ...m, saveTags: v }))}
            />
            <ToggleRow
              label="Captions"
              checked={metadata.saveCaptions}
              onChange={(v) =>
                setMetadata((m) => ({ ...m, saveCaptions: v }))
              }
            />
          </Section>

          <div className="border border-border p-3">
            <div className="grid grid-cols-2 gap-4">
              <div>
                <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-1.5">
                  Download size
                </div>
                <div className="text-base font-bold font-mono tabular-nums">
                  ~{formatGb(estimatedGb)}
                </div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-1.5">
                  Bandwidth cost
                </div>
                <div className="text-base font-bold font-mono tabular-nums">
                  ~{formatUsd(estimatedCostUsd)}
                </div>
              </div>
            </div>
            <div className="text-xs text-muted-foreground mt-3 font-mono tabular-nums">
              {matchingVideos.length} of {videos.length} videos
            </div>
          </div>
        </SheetBody>

        <SheetFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            onClick={() => void runBulkDownload()}
            disabled={matchingVideos.length === 0}
          >
            <Download />
            Download {matchingVideos.length}
          </Button>
        </SheetFooter>
        </>
        )}
      </SheetContent>
    </Sheet>
  )
}

function Section({
  label,
  children,
}: {
  label: string
  children: React.ReactNode
}) {
  return (
    <section>
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">
        {label}
      </div>
      <div className="space-y-3">{children}</div>
    </section>
  )
}

function ToggleRow({
  label,
  description,
  checked,
  disabled,
  onChange,
}: {
  label: string
  description?: string
  checked: boolean
  disabled?: boolean
  onChange?: (v: boolean) => void
}) {
  return (
    <div className="flex items-center justify-between gap-4">
      <div className="min-w-0 flex-1">
        <div
          className={
            "text-sm font-semibold " +
            (disabled ? "text-muted-foreground" : "")
          }
        >
          {label}
        </div>
        {description && (
          <div className="text-xs text-muted-foreground mt-0.5">
            {description}
          </div>
        )}
      </div>
      <Switch
        checked={checked}
        onCheckedChange={onChange ?? (() => {})}
        disabled={disabled}
        aria-label={label}
      />
    </div>
  )
}

function ScopeButton({
  active,
  onClick,
  children,
  isRight,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
  isRight?: boolean
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "h-10 text-sm font-bold border cursor-pointer",
        isRight ? "border-l-0" : undefined,
        active
          ? "bg-white text-black border-white"
          : "border-border text-foreground"
      )}
    >
      {children}
    </button>
  )
}
