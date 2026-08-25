import * as React from "react"
import JSZip from "jszip"
import {
  AlertTriangle,
  Captions,
  Check,
  ChevronDown,
  CircleSlash,
  Clock,
  Copy,
  Download,
  ExternalLink,
  Eye,
  FileText,
  Heart,
  Lock,
  MessageSquare,
  Pencil,
  Pin,
  RefreshCw,
  RotateCw,
  Tag,
  ThumbsUp,
  Trash2,
} from "lucide-react"
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetBody,
  SheetFooter,
} from "./ui/sheet"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "./ui/dialog"
import { Tabs, TabsList, TabsTrigger, TabsContent } from "./ui/tabs"
import { Button } from "./ui/button"
import { Badge } from "./ui/badge"
import { Progress } from "./ui/progress"
import { useToast } from "./ui/toast"
import type {
  ChannelArchiveSettings,
  Video,
} from "@/lib/types"
import {
  formatBytes,
  formatCount,
  formatDuration,
  formatFullDate,
  formatRelativeDate,
} from "@/lib/format"
import {
  triggerBlobDownload,
  triggerHrefDownload,
} from "@/lib/download"
import { isQualityOutdated } from "@/lib/estimates"
import { cn } from "@/lib/utils"

type Props = {
  video: Video | null
  open: boolean
  onOpenChange: (open: boolean) => void
  settings: ChannelArchiveSettings
  /** False when the channel cannot sync comments at all (no connected
      Google account), so the preview is hidden rather than rendering a
      panel that can only ever be empty. */
  commentsAvailable?: boolean
  onSync?: (v: Video) => void
  onRetry?: (v: Video) => void
}

type FieldHistoryEntry = {
  value: unknown
  capturedAt: string
  lastSeenAt: string
  supersededAt: string
  // Thumbnail entries only: presigned R2 URL for the historical image.
  downloadUrl?: string | null
}

type FieldHistoryField = {
  current: {
    value: unknown
    since: string | null
    lastConfirmedAt: string | null
  }
  history: FieldHistoryEntry[]
}

type FieldHistory = {
  title: FieldHistoryField
  description: FieldHistoryField
  tags: FieldHistoryField
  thumbnail: FieldHistoryField
  privacy: FieldHistoryField
}

type DownloadParts = {
  title: string
  safeTitle: string
  video: {
    url: string | null
    filename: string
    sizeBytes: number | null
    available: boolean
  }
  thumbnail: {
    url: string | null
    filename: string
    available: boolean
  }
  metadata: {
    filename: string
    available: boolean
    data: Record<string, unknown>
  }
  captions: {
    language: string
    url: string
    filename: string
  }[]
  expiresInSec: number
}

export function VideoDetailPanel({
  video,
  open,
  onOpenChange,
  settings,
  commentsAvailable,
  onSync,
  onRetry,
}: Props) {
  if (!video) return null
  const isDeleted = video.status === "deleted_on_youtube"
  const hasLocal = video.localPath !== null
  const downloadable = video.status === "archived" || (isDeleted && hasLocal)
  const unrecoverable = isDeleted && !hasLocal

  const [copied, setCopied] = React.useState(false)
  const [tagsCopied, setTagsCopied] = React.useState(false)
  const copyTags = () => {
    if (video.tags.length === 0) return
    // Comma-separated joined - that's the format YouTube Studio's tag
    // field accepts on paste, and the most common downstream use case.
    void navigator.clipboard.writeText(video.tags.join(", ")).then(() => {
      setTagsCopied(true)
      window.setTimeout(() => setTagsCopied(false), 1200)
    })
  }
  const copyUrl = () => {
    const url = `https://www.youtube.com/watch?v=${video.id}`
    void navigator.clipboard.writeText(url).then(() => {
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1200)
    })
  }

  // Download picker state: which parts the user wants, whether the
  // picker is open, and a loading flag while the download is in
  // flight. Defaults to everything checked - matches user intent
  // of "give me everything" on first click.
  const { toast } = useToast()
  const [downloadOpen, setDownloadOpen] = React.useState(false)
  const [downloadOpts, setDownloadOpts] = React.useState({
    video: true,
    thumbnail: true,
    metadata: true,
    captions: true,
  })
  const [downloadBusy, setDownloadBusy] = React.useState(false)
  // Pre-fetched part info (presigned URLs + metadata blob). Loaded
  // lazily the first time the user opens the picker, then reused for
  // subsequent downloads while the panel stays open. URLs expire in
  // 5 min, so we refresh if the picker is stale.
  const [parts, setParts] = React.useState<DownloadParts | null>(null)
  const [partsLoadedAt, setPartsLoadedAt] = React.useState<number>(0)

  // Field history (versioned metadata snapshots). Lazy-loaded the
  // first time the panel renders for a given video, then reused
  // across opens. Thumbnail history is presigned for 5 min - if the
  // user opens the dialog later we refresh.
  const [history, setHistory] = React.useState<FieldHistory | null>(null)
  const [historyLoadedAt, setHistoryLoadedAt] = React.useState<number>(0)
  const [thumbHistoryOpen, setThumbHistoryOpen] = React.useState(false)
  // Which text-field history dialog is open. Single dialog component
  // (TextFieldHistoryDialog below) handles title / description / tags
  // / privacy by switching on the field name. Null means closed.
  const [textHistoryField, setTextHistoryField] = React.useState<
    "title" | "description" | "tags" | "privacy" | null
  >(null)

  const loadHistory = React.useCallback(async (): Promise<FieldHistory | null> => {
    try {
      const res = await fetch(
        `/api/youtube/videos/${encodeURIComponent(video.id)}/field-history`,
        { credentials: "include" }
      )
      if (!res.ok) return null
      const data = (await res.json()) as FieldHistory
      setHistory(data)
      setHistoryLoadedAt(Date.now())
      return data
    } catch {
      return null
    }
  }, [video.id])

  // Load history once when the panel opens for a given video. Cheap
  // (one GET, presigned URLs on the response) so we always fetch.
  React.useEffect(() => {
    if (!open) return
    if (history && Date.now() - historyLoadedAt < 4 * 60_000) return
    void loadHistory()
    // We intentionally only re-fetch when (open, video.id) changes;
    // the stale check above handles the URL expiry case.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, video.id])

  const thumbHistoryCount = history?.thumbnail.history.length ?? 0
  const titleHistoryCount = history?.title.history.length ?? 0
  const descHistoryCount = history?.description.history.length ?? 0
  const tagsHistoryCount = history?.tags.history.length ?? 0
  const privacyHistoryCount = history?.privacy.history.length ?? 0

  const loadParts = React.useCallback(async (): Promise<DownloadParts | null> => {
    try {
      const res = await fetch(
        `/api/youtube/videos/${encodeURIComponent(video.id)}/download-parts`,
        { credentials: "include" }
      )
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}))
        throw new Error(
          typeof detail?.detail === "string" ? detail.detail : `HTTP ${res.status}`
        )
      }
      const data = (await res.json()) as DownloadParts
      setParts(data)
      setPartsLoadedAt(Date.now())
      return data
    } catch (e) {
      toast({
        title: "Couldn't prepare download",
        description: e instanceof Error ? e.message : "Try again in a moment.",
        variant: "error",
      })
      return null
    }
  }, [video.id, toast])

  const togglePicker = () => {
    const next = !downloadOpen
    setDownloadOpen(next)
    // Lazy load on first open, or refresh if parts older than 4 min
    // (URLs expire at 5 min; rebuild a tick before that).
    if (next && (!parts || Date.now() - partsLoadedAt > 4 * 60_000)) {
      void loadParts()
    }
  }

  const hasCaptions = video.captionLanguages.length > 0
  const selectedCount =
    (downloadOpts.video ? 1 : 0) +
    (downloadOpts.thumbnail ? 1 : 0) +
    (downloadOpts.metadata ? 1 : 0) +
    (downloadOpts.captions && hasCaptions ? 1 : 0)

  const runDownload = async () => {
    if (selectedCount === 0) return
    setDownloadBusy(true)
    try {
      // Refresh parts if stale - URLs expire at 5 min.
      let live = parts
      if (!live || Date.now() - partsLoadedAt > 4 * 60_000) {
        live = await loadParts()
      }
      if (!live) return

      const wantVideo = downloadOpts.video && live.video.available
      const wantThumb = downloadOpts.thumbnail && live.thumbnail.available
      const wantMeta = downloadOpts.metadata && live.metadata.available
      const wantCaps = downloadOpts.captions && live.captions.length > 0
      // For totalChosen we count captions as ONE bundle even if there
      // are multiple language tracks - a single VTT downloads
      // standalone, multiple go in a ZIP together. Either way it's
      // one "part" the user selected.
      const totalChosen =
        (wantVideo ? 1 : 0) +
        (wantThumb ? 1 : 0) +
        (wantMeta ? 1 : 0) +
        (wantCaps ? 1 : 0)
      if (totalChosen === 0) {
        toast({
          title: "Nothing to download",
          description: "Selected parts aren't archived for this video.",
          variant: "error",
        })
        return
      }

      // Single-item: skip the ZIP, save the file directly. (Only
      // applies when there's literally one file - if captions are
      // the only chosen part but there are 2+ tracks, we still ZIP.)
      const singleCaption = wantCaps && live.captions.length === 1
      if (totalChosen === 1 && (!wantCaps || singleCaption)) {
        if (wantVideo && live.video.url) {
          triggerHrefDownload(live.video.url, live.video.filename)
        } else if (wantThumb && live.thumbnail.url) {
          triggerHrefDownload(live.thumbnail.url, live.thumbnail.filename)
        } else if (wantMeta) {
          const blob = new Blob(
            [JSON.stringify(live.metadata.data, null, 2)],
            { type: "application/json" }
          )
          triggerBlobDownload(blob, live.metadata.filename)
        } else if (singleCaption) {
          const cap = live.captions[0]!
          triggerHrefDownload(cap.url, cap.filename)
        }
        return
      }

      // Multi-item: client-side ZIP. Bytes still flow R2 -> browser
      // direct (free egress); ZIP build is in-memory here.
      const zip = new JSZip()
      if (wantVideo && live.video.url) {
        const res = await fetch(live.video.url)
        if (!res.ok) throw new Error(`video fetch HTTP ${res.status}`)
        zip.file(live.video.filename, await res.blob())
      }
      if (wantThumb && live.thumbnail.url) {
        const res = await fetch(live.thumbnail.url)
        if (!res.ok) throw new Error(`thumbnail fetch HTTP ${res.status}`)
        zip.file(live.thumbnail.filename, await res.blob())
      }
      if (wantMeta) {
        zip.file(
          live.metadata.filename,
          JSON.stringify(live.metadata.data, null, 2)
        )
      }
      if (wantCaps) {
        // Captions live in their own folder so they don't clutter the
        // root next to the video/thumbnail/metadata. Each named by
        // language code so users can find specific tracks at a glance.
        for (const cap of live.captions) {
          const res = await fetch(cap.url)
          if (!res.ok) throw new Error(`caption ${cap.language} HTTP ${res.status}`)
          zip.file(`captions/${cap.filename}`, await res.blob())
        }
      }
      const zipBlob = await zip.generateAsync({ type: "blob" })
      triggerBlobDownload(zipBlob, `${live.safeTitle}.zip`)
    } catch (e) {
      toast({
        title: "Download failed",
        description: e instanceof Error ? e.message : "Try again in a moment.",
        variant: "error",
      })
    } finally {
      setDownloadBusy(false)
    }
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="max-w-[520px] detail-selectable" hideClose>
        <SheetHeader>
          <SheetTitle className="leading-snug pr-24">
            {video.title}
            {titleHistoryCount > 0 && (
              <button
                type="button"
                onClick={() => setTextHistoryField("title")}
                aria-label="View title history"
                title={`View ${titleHistoryCount} previous ${
                  titleHistoryCount === 1 ? "title" : "titles"
                }`}
                className="ml-2 inline-flex items-center align-middle text-muted-foreground hover:text-foreground cursor-pointer"
              >
                <Clock className="size-3.5" />
              </button>
            )}
          </SheetTitle>
          <a
            href={`https://www.youtube.com/watch?v=${video.id}`}
            target="_blank"
            rel="noopener noreferrer"
            aria-label="Open on YouTube"
            className="absolute right-14 top-3 size-8 flex items-center justify-center text-muted-foreground hover:text-foreground cursor-pointer"
          >
            <ExternalLink className="size-4" />
          </a>
          <button
            type="button"
            onClick={copyUrl}
            aria-label={copied ? "Copied YouTube URL" : "Copy YouTube URL"}
            className="absolute right-4 top-3 size-8 flex items-center justify-center text-muted-foreground hover:text-foreground cursor-pointer"
          >
            {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
          </button>
        </SheetHeader>

        <SheetBody className="space-y-6">
          {/* Thumbnail - only when the toggle is on AND we actually hold
              the image. Without the second test this rendered an <img>
              with an empty src for every un-synced video, which paints a
              broken-image box. Not backed up means nothing to show. */}
          {settings.saveThumbnail && !!video.thumbnailUrl && (
          <div className="relative aspect-video overflow-hidden border border-border bg-muted">
            <img
              draggable={false}
              src={video.thumbnailUrl}
              alt=""
              referrerPolicy="no-referrer"
              className="size-full object-cover"
            />
            {/* History affordance: only appears when there's actual
                history to show. Click opens the all-versions dialog. */}
            {thumbHistoryCount > 0 && (
              <button
                type="button"
                onClick={() => setThumbHistoryOpen(true)}
                className="absolute top-2 right-2 inline-flex items-center gap-1.5 bg-black/80 border border-white/40 px-2 py-1 text-xs font-semibold text-white cursor-pointer hover:bg-black"
                aria-label="View thumbnail history"
              >
                <Clock className="size-3" />
                {thumbHistoryCount === 1
                  ? "1 previous"
                  : `${thumbHistoryCount} previous`}
              </button>
            )}
            {video.status === "syncing" && (
              <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/90 via-black/60 to-transparent p-3">
                <div className="flex items-center justify-between text-xs text-white mb-1.5">
                  <span>Syncing</span>
                  <span className="font-mono tabular-nums">
                    {Math.round((video.syncProgress ?? 0) * 100)}%
                  </span>
                </div>
                <Progress
                  value={video.syncProgress ?? 0}
                  className="bg-white/20 h-1"
                />
              </div>
            )}
          </div>
          )}

          <ThumbnailHistoryDialog
            open={thumbHistoryOpen}
            onOpenChange={setThumbHistoryOpen}
            currentUrl={video.thumbnailUrl}
            history={history?.thumbnail ?? null}
            onRefresh={() => void loadHistory()}
          />

          <TextFieldHistoryDialog
            field={textHistoryField}
            history={
              textHistoryField && history ? history[textHistoryField] : null
            }
            onClose={() => setTextHistoryField(null)}
          />


          {/* Status pills - moved off the thumbnail so they don't
              obscure the image. Same variants as VideoCard's grid view
              so Private reads red, Unlisted amber, etc. */}
          <div className="flex flex-wrap gap-1.5">
            {video.status === "archived" && (
              <Badge variant="success">Archived</Badge>
            )}
            {video.status === "archived" &&
              isQualityOutdated(
                {
                  resolution: settings.maxResolution,
                  codec: settings.codecPreference,
                },
                {
                  resolution: video.archivedMaxResolution,
                  codec: video.archivedCodecPreference,
                },
              ) && (
                <Badge
                  variant="warning"
                  title={`Archived at ${video.archivedMaxResolution} / ${video.archivedCodecPreference}; channel setting is now ${settings.maxResolution} / ${settings.codecPreference}. Re-sync to replace.`}
                >
                  Outdated
                </Badge>
              )}
            {isDeleted && hasLocal && (
              <Badge variant="warning">
                <AlertTriangle />
                Unavailable · local copy
              </Badge>
            )}
            {unrecoverable && (
              <Badge variant="destructive">
                <CircleSlash />
                Unavailable · no copy
              </Badge>
            )}
            {video.status === "failed" && (
              <Badge variant="destructive">Download failed</Badge>
            )}
            {video.visibility === "open" && (
              <Badge
                variant="outline"
                title="Archive visibility: anyone subscribed can watch this here. We captured it while it was public, so it stays Open even if YouTube privates the source."
              >
                <Eye />
                Open
              </Badge>
            )}
            {video.visibility === "sealed" && (
              <Badge
                variant="outline"
                title="Archive visibility: only the authenticated channel owner can watch this."
              >
                <Lock />
                Sealed
              </Badge>
            )}
            {video.privacy === "unlisted" && (
              <Badge variant="warning">Unlisted</Badge>
            )}
            {video.privacy === "private" && (
              <Badge variant="private">Private</Badge>
            )}
            {video.privacy === "members" && (
              <Badge variant="members">Members</Badge>
            )}
            {privacyHistoryCount > 0 && (
              <HistoryAffordance
                count={privacyHistoryCount}
                onClick={() => setTextHistoryField("privacy")}
                fieldLabel="privacy state"
                inline
              />
            )}
          </div>

          {/* Primary action: download picker. Click expands a panel
              with one checkbox per available part; clicking the
              "Download" inside that panel kicks off the actual save.
              Locked while a download is mid-flight so the user can't
              accidentally collapse the picker and lose sight of the
              in-progress state (or fire a second simultaneous job). */}
          {downloadable && (
            <div>
              <Button
                size="lg"
                className="w-full h-12 text-base"
                onClick={togglePicker}
                disabled={downloadBusy}
              >
                <Download />
                {downloadBusy
                  ? selectedCount > 1
                    ? "Packaging…"
                    : "Downloading…"
                  : "Download"}
                <ChevronDown
                  className={cn(
                    "ml-1 size-4",
                    downloadOpen && "rotate-180"
                  )}
                />
              </Button>
              {downloadOpen && (
                <div className="mt-3 border border-border p-4 space-y-3">
                  <DownloadCheckbox
                    label="Video"
                    sub={
                      parts?.video.sizeBytes != null
                        ? formatBytes(parts.video.sizeBytes)
                        : video.fileSizeBytes
                        ? formatBytes(video.fileSizeBytes)
                        : undefined
                    }
                    checked={downloadOpts.video}
                    disabled={parts ? !parts.video.available : false}
                    onChange={(v) =>
                      setDownloadOpts((o) => ({ ...o, video: v }))
                    }
                  />
                  <DownloadCheckbox
                    label="Thumbnail"
                    checked={downloadOpts.thumbnail}
                    disabled={parts ? !parts.thumbnail.available : false}
                    onChange={(v) =>
                      setDownloadOpts((o) => ({ ...o, thumbnail: v }))
                    }
                  />
                  <DownloadCheckbox
                    label="Metadata"
                    sub="JSON"
                    checked={downloadOpts.metadata}
                    onChange={(v) =>
                      setDownloadOpts((o) => ({ ...o, metadata: v }))
                    }
                  />
                  <DownloadCheckbox
                    label="Captions"
                    sub={
                      hasCaptions
                        ? video.captionLanguages.length === 1
                          ? video.captionLanguages[0]
                          : `${video.captionLanguages.length} tracks`
                        : undefined
                    }
                    checked={downloadOpts.captions}
                    disabled={
                      !hasCaptions ||
                      (parts ? parts.captions.length === 0 : false)
                    }
                    onChange={(v) =>
                      setDownloadOpts((o) => ({ ...o, captions: v }))
                    }
                  />
                  <Button
                    className="w-full"
                    onClick={runDownload}
                    disabled={downloadBusy || selectedCount === 0}
                  >
                    <Download />
                    {downloadBusy
                      ? selectedCount > 1
                        ? "Packaging…"
                        : "Downloading…"
                      : selectedCount > 1
                      ? `Download (${selectedCount} as ZIP)`
                      : "Download"}
                  </Button>
                </div>
              )}
            </div>
          )}
          {video.status === "discovered" && (
            <Button
              size="lg"
              className="w-full h-12 text-base"
              onClick={() => onSync?.(video)}
            >
              <RefreshCw />
              Sync
            </Button>
          )}
          {video.status === "failed" && (
            <Button
              variant="outline"
              size="lg"
              className="w-full h-12 text-base"
              onClick={() => onRetry?.(video)}
            >
              <RotateCw />
              Retry sync
            </Button>
          )}
          {/* Phrased as what we observed, not as a verdict: the same
              detection fires for takedowns, region blocks, age gates and
              bot-checks against our scraper, so "no longer on YouTube" was
              a claim we can't stand behind. The part we can state flatly is
              the half that's about us - there's no copy on our side. */}
          {unrecoverable && (
            <div className="text-sm text-muted-foreground text-center py-2">
              We couldn't see this video on YouTube when we last checked, and
              there's no copy in your archive.
            </div>
          )}

          {/* Meta grid - required fields first, then the one
              user-toggleable field (Views) at the end. Comments live
              on their own separate surface, not nested in metadata. */}
          <div className="grid grid-cols-2 gap-x-4 gap-y-3 text-sm">
            <Meta label="Uploaded" value={formatFullDate(video.uploadDate)} />
            <Meta
              label="Duration"
              value={formatDuration(video.durationSec)}
            />
            <Meta label="Type" value={prettyVideoType(video.type)} />
            {video.archivedAt && (
              <Meta
                label="Archived"
                value={formatRelativeDate(video.archivedAt)}
              />
            )}
            {video.lastYoutubeCheckAt && (
              <Meta
                label="Last checked"
                value={formatRelativeDate(video.lastYoutubeCheckAt)}
              />
            )}
            {video.deletedOnYoutubeAt && (
              // Label is the bare state, not "Unavailable since": Meta stacks
              // a caption over a value, and every sibling row here reads as
              // event over when ("Archived" / "4 days ago"). "Unavailable
              // since" over "4 days ago" would break that pattern and read as
              // broken English.
              <Meta
                label="Unavailable"
                value={formatRelativeDate(video.deletedOnYoutubeAt)}
                highlight
              />
            )}
            {settings.saveViewCount && (
              <Meta
                label="Views"
                value={formatCount(video.viewCount)}
                icon={<Eye className="size-3" />}
              />
            )}
          </div>

          {/* File info (only when archived). Rendered inline without a
              section header - size/resolution are essential enough to
              live as flat rows in the panel. Order: identity (format,
              size, hash) → video stream → audio stream. Hidden rows
              when a field is missing (worker may have skipped
              ffprobe). */}
          {hasLocal && (
            <div>
              <div className="space-y-2 text-sm">
                {!!video.fileSizeBytes && (
                  <KVRow
                    label="Size"
                    value={formatBytes(video.fileSizeBytes)}
                    mono
                  />
                )}
                {video.videoResolution && (
                  <KVRow
                    label="Resolution"
                    value={
                      video.videoResolution +
                      aspectRatioSuffix(video.videoResolution)
                    }
                    mono
                  />
                )}
              </div>
              <AdvancedFileDetails video={video} />
            </div>
          )}

          {/* Description — gated on settings */}
          {settings.saveDescription && video.description && (
            <Section
              label="Description"
              icon={<FileText className="size-3" />}
              action={
                descHistoryCount > 0 ? (
                  <HistoryAffordance
                    count={descHistoryCount}
                    onClick={() => setTextHistoryField("description")}
                    fieldLabel="description"
                  />
                ) : undefined
              }
            >
              {/* break-words, like every other pre-wrap block here. Without it
                  a long unbroken token never wraps, and YouTube
                  descriptions are mostly long URLs - one of them widened
                  the panel and let the whole sheet scroll sideways into
                  empty space. */}
              <p className="text-sm text-neutral-300 leading-relaxed whitespace-pre-wrap break-words">
                {video.description}
              </p>
            </Section>
          )}

          {/* Tags — gated on settings. Header gets a small Copy
              button that puts them on the clipboard as a comma-
              separated string (the format YouTube Studio expects
              when pasted into its tag field). */}
          {settings.saveTags && video.tags.length > 0 && (
            <Section
              label="Tags"
              icon={<Tag className="size-3" />}
              action={
                <div className="flex items-center gap-3">
                  {tagsHistoryCount > 0 && (
                    <HistoryAffordance
                      count={tagsHistoryCount}
                      onClick={() => setTextHistoryField("tags")}
                      fieldLabel="tags"
                    />
                  )}
                  <button
                    type="button"
                    onClick={copyTags}
                    aria-label={
                      tagsCopied ? "Copied tags" : "Copy tags to clipboard"
                    }
                    className="flex items-center gap-1 text-muted-foreground hover:text-foreground cursor-pointer normal-case tracking-normal text-[10px] font-semibold"
                  >
                    {tagsCopied ? (
                      <>
                        <Check className="size-3" />
                        Copied
                      </>
                    ) : (
                      <>
                        <Copy className="size-3" />
                        Copy
                      </>
                    )}
                  </button>
                </div>
              }
            >
              <div className="flex flex-wrap gap-1.5">
                {video.tags.map((t) => (
                  <span
                    key={t}
                    className="text-xs font-semibold border border-border px-2 py-0.5 text-neutral-300"
                  >
                    {t}
                  </span>
                ))}
              </div>
            </Section>
          )}

          {/* Captions — surfaced for archived videos. Manually-authored
              caption tracks only (auto-generated speech-to-text is
              intentionally excluded). Empty list rendered explicitly as
              "None" so the user can see we checked. Pre-captions-rollout
              archives default to empty here; running the Captions
              backfill from the Sync panel populates them. */}
          {video.status === "archived" && (
            <Section
              label="Captions"
              icon={<Captions className="size-3" />}
            >
              {video.captionLanguages.length > 0 ? (
                <div className="flex flex-wrap gap-1.5">
                  {video.captionLanguages.map((lang) => (
                    <span
                      key={lang}
                      className="text-xs font-semibold border border-border px-2 py-0.5 text-neutral-300"
                    >
                      {lang}
                    </span>
                  ))}
                </div>
              ) : (
                <div className="text-sm text-muted-foreground">None</div>
              )}
            </Section>
          )}

          {/* Comments preview — lazy-loaded three-tab strip (recent /
              top / deleted) for archived videos when the channel has
              comments sync on. Full search lives on its own page;
              this is just the at-a-glance peek inside the detail
              panel. */}
          {video.status === "archived" &&
            settings.syncComments &&
            commentsAvailable !== false && (
            <CommentsPreview videoId={video.id} />
          )}
        </SheetBody>

        <SheetFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Close
          </Button>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  )
}

function Section({
  label,
  icon,
  action,
  children,
}: {
  label: string
  icon?: React.ReactNode
  // Optional right-aligned slot in the section header for things like
  // a 'Copy' button. Sits on the same baseline as the label.
  action?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <section>
      <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-2">
        {icon}
        <span>{label}</span>
        {action && <div className="ml-auto">{action}</div>}
      </div>
      {children}
    </section>
  )
}

function Meta({
  label,
  value,
  icon,
  highlight,
}: {
  label: string
  value: string
  icon?: React.ReactNode
  highlight?: boolean
}) {
  return (
    <div>
      <div className="flex items-center gap-1 text-[10px] uppercase tracking-wider text-muted-foreground font-semibold">
        {icon}
        <span>{label}</span>
      </div>
      <div
        className={cn(
          "mt-0.5 text-sm font-semibold font-mono tabular-nums",
          highlight ? "text-amber-400" : "text-foreground"
        )}
      >
        {value}
      </div>
    </div>
  )
}

function KVRow({
  label,
  value,
  mono,
}: {
  label: string
  value: string
  mono?: boolean
}) {
  // Label column is sized to fit the longest label we currently use
  // ('VIDEO BITRATE' / 'AUDIO BITRATE' uppercased) so it never wraps.
  // w-28 (112px) is the threshold; anything narrower wraps these into
  // two lines and the value's vertical alignment goes off. If we ever
  // add a longer label, bump this together with it.
  return (
    <div className="flex items-start gap-3">
      <div className="w-28 shrink-0 text-[11px] uppercase tracking-wider text-muted-foreground font-semibold pt-0.5">
        {label}
      </div>
      <div
        className={cn(
          "min-w-0 flex-1 text-sm text-neutral-300 break-all",
          mono && "font-mono"
        )}
      >
        {value}
      </div>
    </div>
  )
}

function DownloadCheckbox({
  label,
  sub,
  checked,
  disabled,
  onChange,
}: {
  label: string
  sub?: string
  checked: boolean
  disabled?: boolean
  onChange: (v: boolean) => void
}) {
  // Split the row into two siblings so only the <label> (checkbox +
  // its text) is the click target. The trailing sub-text sits outside
  // the label, so clicking 130.1 MB / JSON doesn't toggle the box -
  // matches the user's expectation that only the named control area
  // is interactive.
  return (
    <div
      className={cn(
        "flex items-center justify-between gap-3 text-sm",
        disabled && "opacity-50"
      )}
    >
      <label
        className={cn(
          "inline-flex items-center gap-3 cursor-pointer select-none",
          disabled && "cursor-not-allowed"
        )}
      >
        <input
          type="checkbox"
          checked={checked}
          disabled={disabled}
          onChange={(e) => onChange(e.currentTarget.checked)}
          className="size-4 accent-foreground cursor-pointer disabled:cursor-not-allowed"
        />
        <span className="font-semibold">{label}</span>
      </label>
      {sub && (
        <span className="text-xs text-muted-foreground font-mono tabular-nums">
          {sub}
        </span>
      )}
    </div>
  )
}

// Programmatic browser download triggers. The href version uses
// <a download href=presignedUrl> so the bytes flow R2 -> browser
// direct (free egress). The blob version handles client-built
// content like the metadata JSON or the multi-part ZIP.

// Map our internal VideoType enum to a display label. Keep this in
// step with whatever the grid view shows in its filter chip so the
// same vocabulary appears everywhere.
/**
 * Collapsible block under the File section that hides all the
 * power-user / compliance fields (codecs, bitrates, hash, container
 * format) behind one click. Most users only care about size +
 * resolution; this keeps those visible while the technical bits stay
 * tucked away until requested.
 *
 * Local state - resets to closed every time the panel reopens, which
 * is the right default since the typical interaction is "click video,
 * skim, move on".
 */
function AdvancedFileDetails({ video }: { video: Video }) {
  const [open, setOpen] = React.useState(false)

  // Only render the collapsible at all when we have at least one
  // advanced field to show. Otherwise this would be an empty toggle.
  const hasAny =
    !!video.videoFormat ||
    !!video.videoCodec ||
    video.videoBitrateKbps != null ||
    !!video.audioCodec ||
    video.audioBitrateKbps != null ||
    !!video.fileSha256
  if (!hasAny) return null

  return (
    <div className="mt-3 border-t border-border/60 pt-3">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-muted-foreground hover:text-foreground font-semibold cursor-pointer"
      >
        <ChevronDown
          className={cn("size-3", open && "rotate-180")}
        />
        Advanced
      </button>
      {open && (
        <div className="space-y-2 text-sm mt-3">
          {video.videoFormat && (
            <KVRow label="Format" value={video.videoFormat} mono />
          )}
          {video.videoCodec && (
            <KVRow
              label="Video codec"
              value={
                video.videoCodec.toUpperCase() +
                (video.videoFps ? ` @ ${video.videoFps}fps` : "")
              }
              mono
            />
          )}
          {video.videoBitrateKbps != null && (
            <KVRow
              label="Video bitrate"
              value={`${video.videoBitrateKbps} kbps`}
              mono
            />
          )}
          {video.audioCodec && (
            <KVRow
              label="Audio codec"
              value={video.audioCodec.toUpperCase()}
              mono
            />
          )}
          {video.audioBitrateKbps != null && (
            <KVRow
              label="Audio bitrate"
              value={`${video.audioBitrateKbps} kbps`}
              mono
            />
          )}
          {video.fileSha256 && (
            <KVRow label="SHA-256" value={video.fileSha256} mono />
          )}
        </div>
      )}
    </div>
  )
}

function prettyVideoType(t: string | null | undefined): string {
  switch (t) {
    case "short":
      return "Short"
    case "live":
    case "livestream":
      return "Livestream"
    case "premiere":
      return "Premiere"
    case "regular":
      return "Regular"
    default:
      return t ?? "Unknown"
  }
}

// Quick aspect-ratio derivation from a "WxH" resolution string. Maps
// to the common human labels (16:9 / 9:16 / 4:3 / 1:1). Falls back to
// the GCD ratio for anything weird. Empty string when input doesn't
// parse, so the caller can string-concat without checking.
function aspectRatioSuffix(resolution: string): string {
  const m = resolution.match(/^(\d+)x(\d+)$/)
  if (!m) return ""
  const w = Number(m[1])
  const h = Number(m[2])
  if (!w || !h) return ""
  const ratio = w / h
  // Known ratios with a small tolerance.
  if (Math.abs(ratio - 16 / 9) < 0.02) return " (16:9)"
  if (Math.abs(ratio - 9 / 16) < 0.02) return " (9:16 vertical)"
  if (Math.abs(ratio - 4 / 3) < 0.02) return " (4:3)"
  if (Math.abs(ratio - 1) < 0.02) return " (1:1 square)"
  if (Math.abs(ratio - 21 / 9) < 0.05) return " (21:9 ultrawide)"
  // Fall back to a reduced fraction via GCD.
  const gcd = (a: number, b: number): number => (b === 0 ? a : gcd(b, a % b))
  const g = gcd(w, h)
  return ` (${w / g}:${h / g})`
}

// download helpers moved to @/lib/download for shared use by
// VideoDetailPanel (single video) and DownloadPanel (bulk).


/**
 * Modal viewer for every archived version of a video's thumbnail.
 *
 * Shows the current thumbnail on top + each historical version below
 * with the timespan it was active for, and a Download button on each.
 *
 * History entries' downloadUrls are 5-minute presigned R2 URLs. We
 * call `onRefresh` lazily (on open + on URL exhaustion) so the dialog
 * always has live URLs to hand to the download triggers.
 */
function ThumbnailHistoryDialog({
  open,
  onOpenChange,
  currentUrl,
  history,
  onRefresh,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
  currentUrl: string
  history: FieldHistoryField | null
  onRefresh: () => void
}) {
  // Whenever the dialog opens, re-fetch so presigned URLs are fresh.
  // Cheap (one GET) and avoids the "I clicked Download an hour later
  // and it 403'd" footgun.
  React.useEffect(() => {
    if (open) onRefresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  // History is most-recent-first from the API; render in that order.
  const entries = history?.history ?? []
  const currentSince = history?.current.since

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <div className="p-6 space-y-5">
          <DialogHeader>
            <DialogTitle>Thumbnail history</DialogTitle>
          </DialogHeader>

          <div className="space-y-4 max-h-[60vh] overflow-y-auto">
            {/* Current */}
            <ThumbHistoryRow
              label="Current"
              sub={
                currentSince
                  ? `Since ${formatFullDate(currentSince)}`
                  : undefined
              }
              imageSrc={currentUrl}
              downloadHref={currentUrl}
              downloadFilename="thumbnail-current.jpg"
              variant="current"
            />

            {/* Historical, most-recent first */}
            {entries.map((e, idx) => {
              const oldValue = e.value as { url?: string } | null
              return (
                <ThumbHistoryRow
                  key={`${e.supersededAt}-${idx}`}
                  label={`Version ${entries.length - idx}`}
                  sub={`Active ${formatFullDate(e.capturedAt)} — ${formatFullDate(
                    e.supersededAt
                  )}`}
                  imageSrc={e.downloadUrl ?? oldValue?.url ?? ""}
                  downloadHref={e.downloadUrl ?? null}
                  downloadFilename={`thumbnail-${e.supersededAt.replace(/[:.]/g, "")}.jpg`}
                  variant="history"
                />
              )
            })}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}

type CommentSort = "new" | "top" | "deleted"

type ApiComment = {
  id: string
  parentCommentId: string | null
  videoId: string
  author: string
  authorChannelId: string | null
  text: string
  likeCount: number
  isEdited: boolean
  isPinned: boolean
  isByUploader: boolean
  viewerRatingLike: boolean
  publishedAt: string | null
  updatedAtRemote: string | null
  firstSeenAt: string
  lastSeenAt: string
  deletedAt: string | null
}

type CommentsResponse = {
  total: number
  limit: number
  offset: number
  comments: ApiComment[]
}

/**
 * Three-tab peek at a single video's archived comments.
 *
 * Tabs:
 *   Recent  - newest first (chronological archive view)
 *   Top     - highest like_count first
 *   Deleted - soft-deleted comments, most-recently-deleted first
 *
 * Each tab loads its own page lazily on first switch + caches the
 * result. The deleted tab is the headline feature - what justifies
 * the price tier for users paying to preserve discussions.
 *
 * Capped at 10 per tab. Channel-wide search + browsing lives at a
 * separate route.
 */
function CommentsPreview({ videoId }: { videoId: string }) {
  const [sort, setSort] = React.useState<CommentSort>("new")
  // Per-sort cache so flipping back to a tab doesn't re-fetch.
  const [data, setData] = React.useState<Record<CommentSort, CommentsResponse | null>>({
    new: null,
    top: null,
    deleted: null,
  })
  const [loading, setLoading] = React.useState<Record<CommentSort, boolean>>({
    new: false,
    top: false,
    deleted: false,
  })

  const load = React.useCallback(
    async (which: CommentSort) => {
      if (data[which] || loading[which]) return
      setLoading((s) => ({ ...s, [which]: true }))
      try {
        const params = new URLSearchParams({
          sort: which,
          limit: "10",
          offset: "0",
        })
        const res = await fetch(
          `/api/youtube/videos/${encodeURIComponent(videoId)}/comments?${params.toString()}`,
          { credentials: "include" }
        )
        if (!res.ok) return
        const json = (await res.json()) as CommentsResponse
        setData((s) => ({ ...s, [which]: json }))
      } catch {
        // Silent - the empty state below covers the "no data" case.
      } finally {
        setLoading((s) => ({ ...s, [which]: false }))
      }
    },
    [videoId, data, loading]
  )

  // Auto-load "new" when the component mounts so the default tab is
  // populated. Other tabs load on click.
  React.useEffect(() => {
    void load("new")
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [videoId])

  const onTabChange = (v: string) => {
    const next = v as CommentSort
    setSort(next)
    void load(next)
  }

  return (
    <Section
      label="Comments"
      icon={<MessageSquare className="size-3" />}
    >
      <Tabs value={sort} onValueChange={onTabChange}>
        <TabsList className="grid grid-cols-3 w-full">
          <TabsTrigger value="new">Recent</TabsTrigger>
          <TabsTrigger value="top">Top</TabsTrigger>
          <TabsTrigger value="deleted">
            Deleted
            {data.deleted && data.deleted.total > 0 && (
              <span className="ml-1.5 text-[10px] font-mono text-muted-foreground">
                {data.deleted.total}
              </span>
            )}
          </TabsTrigger>
        </TabsList>
        {(["new", "top", "deleted"] as const).map((which) => (
          <TabsContent key={which} value={which} className="mt-3">
            <CommentList
              data={data[which]}
              loading={loading[which]}
              variant={which}
            />
          </TabsContent>
        ))}
      </Tabs>
    </Section>
  )
}

function CommentList({
  data,
  loading,
  variant,
}: {
  data: CommentsResponse | null
  loading: boolean
  variant: CommentSort
}) {
  if (loading && data === null) {
    return <div className="text-sm text-muted-foreground py-3">Loading…</div>
  }
  if (data === null) {
    return null
  }
  if (data.comments.length === 0) {
    const msg =
      variant === "deleted"
        ? "No deleted comments yet. Once a rescan detects a comment that's gone from YouTube, it'll show up here."
        : "No comments archived yet for this video."
    return (
      <div className="text-sm text-muted-foreground py-3 leading-relaxed">
        {msg}
      </div>
    )
  }
  return (
    <div className="space-y-3">
      {data.comments.map((c) => (
        <CommentItem key={c.id} c={c} />
      ))}
      {data.total > data.comments.length && (
        <div className="text-[11px] text-muted-foreground pt-1">
          Showing {data.comments.length} of {data.total.toLocaleString()} — full
          browsing on the channel comments page.
        </div>
      )}
    </div>
  )
}

function CommentItem({ c }: { c: ApiComment }) {
  const isDeleted = !!c.deletedAt
  return (
    <div
      className={cn(
        "border border-border p-3 space-y-1.5",
        isDeleted && "opacity-70"
      )}
    >
      <div className="flex items-center gap-2 flex-wrap text-xs">
        <span className="font-semibold text-foreground">{c.author}</span>
        {c.isByUploader && (
          <Badge variant="success" className="text-[10px] px-1.5 py-0">
            Uploader
          </Badge>
        )}
        {c.isPinned && (
          <span
            className="text-muted-foreground"
            title="Pinned by uploader"
          >
            <Pin className="size-3" />
          </span>
        )}
        {c.viewerRatingLike && (
          <span
            className="text-muted-foreground"
            title="Hearted by uploader"
          >
            <Heart className="size-3" />
          </span>
        )}
        {c.isEdited && (
          <span
            className="text-muted-foreground"
            title="Edited"
          >
            <Pencil className="size-3" />
          </span>
        )}
        {c.publishedAt && (
          <span className="text-muted-foreground font-mono tabular-nums">
            {formatRelativeDate(c.publishedAt)}
          </span>
        )}
        <span className="ml-auto inline-flex items-center gap-1 text-muted-foreground font-mono tabular-nums">
          <ThumbsUp className="size-3" />
          {c.likeCount.toLocaleString()}
        </span>
      </div>
      <div className="text-sm whitespace-pre-wrap break-words text-neutral-300 leading-relaxed">
        {c.text}
      </div>
      {isDeleted && (
        <div className="flex items-center gap-1.5 pt-1 text-[11px] text-amber-400 font-mono tabular-nums">
          <Trash2 className="size-3" />
          Deleted {formatRelativeDate(c.deletedAt!)}
        </div>
      )}
    </div>
  )
}


function ThumbHistoryRow({
  label,
  sub,
  imageSrc,
  downloadHref,
  downloadFilename,
  variant,
}: {
  label: string
  sub?: string
  imageSrc: string
  downloadHref: string | null
  downloadFilename: string
  variant: "current" | "history"
}) {
  return (
    <div className="border border-border">
      <div className="relative aspect-video overflow-hidden bg-muted">
        {imageSrc ? (
          <img
            draggable={false}
            src={imageSrc}
            alt=""
            referrerPolicy="no-referrer"
            className="size-full object-cover"
          />
        ) : (
          <div className="size-full flex items-center justify-center text-xs text-muted-foreground">
            Unavailable
          </div>
        )}
      </div>
      <div className="flex items-center justify-between gap-3 px-3 py-2">
        <div className="min-w-0">
          <div
            className={cn(
              "text-xs font-semibold uppercase tracking-wider",
              variant === "current"
                ? "text-foreground"
                : "text-muted-foreground"
            )}
          >
            {label}
          </div>
          {sub && (
            <div className="text-[11px] text-muted-foreground mt-0.5 font-mono tabular-nums">
              {sub}
            </div>
          )}
        </div>
        <button
          type="button"
          onClick={() => {
            if (!downloadHref) return
            triggerHrefDownload(downloadHref, downloadFilename)
          }}
          disabled={!downloadHref}
          className="inline-flex items-center gap-1 text-xs font-semibold cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed hover:text-foreground text-muted-foreground"
          aria-label={`Download ${label}`}
        >
          <Download className="size-3" />
          Download
        </button>
      </div>
    </div>
  )
}


/**
 * Small clock-icon button that opens a field's history dialog.
 *
 * Two variants:
 *   - Section header (default): icon + "Nx" count. Sits in the action
 *     slot of a Section header next to other actions like Copy.
 *   - inline: a slim pill with "Nx previous" text. Used in the status
 *     pills row for the privacy field so it reads as part of the
 *     visible chrome rather than tucked in a section header.
 *
 * Only render when count > 0 - no history means no affordance.
 */
function HistoryAffordance({
  count,
  onClick,
  fieldLabel,
  inline,
}: {
  count: number
  onClick: () => void
  fieldLabel: string
  inline?: boolean
}) {
  const ariaLabel = `View ${count} previous ${
    count === 1 ? fieldLabel : `${fieldLabel}s`
  }`
  if (inline) {
    return (
      <button
        type="button"
        onClick={onClick}
        aria-label={ariaLabel}
        className="inline-flex items-center gap-1.5 border border-border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground hover:text-foreground hover:border-foreground cursor-pointer"
      >
        <Clock className="size-3" />
        {count} previous
      </button>
    )
  }
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={ariaLabel}
      title={ariaLabel}
      className="flex items-center gap-1 text-muted-foreground hover:text-foreground cursor-pointer normal-case tracking-normal text-[10px] font-semibold"
    >
      <Clock className="size-3" />
      {count}x
    </button>
  )
}


/**
 * Modal viewer for chronological history of a text/scalar field
 * (title, description, tags, privacy). One dialog component handles
 * all four field types - the field name decides the rendering style.
 *
 * Title in the modal mirrors the field. Each history entry shows:
 *   - the past value (formatted per field type)
 *   - the timespan it was active for
 *
 * Closing is via the dialog's onOpenChange (Escape, X button, outside
 * click). State is single-source-of-truth via the `field` prop being
 * null = closed, set = open.
 */
function TextFieldHistoryDialog({
  field,
  history,
  onClose,
}: {
  field: "title" | "description" | "tags" | "privacy" | null
  history: FieldHistoryField | null
  onClose: () => void
}) {
  const open = field !== null
  const entries = history?.history ?? []
  const currentValue = history?.current.value
  const currentSince = history?.current.since

  const titleMap: Record<NonNullable<typeof field>, string> = {
    title: "Title history",
    description: "Description history",
    tags: "Tags history",
    privacy: "Privacy history",
  }

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) onClose() }}>
      <DialogContent className="max-w-xl">
        <div className="p-6 space-y-5">
          <DialogHeader>
            <DialogTitle>
              {field ? titleMap[field] : "History"}
            </DialogTitle>
          </DialogHeader>

          <div className="space-y-3 max-h-[60vh] overflow-y-auto">
            {/* Current value */}
            {field && (
              <TextHistoryRow
                field={field}
                label="Current"
                sub={
                  currentSince
                    ? `Since ${formatFullDate(currentSince)}`
                    : undefined
                }
                value={currentValue}
                variant="current"
              />
            )}
            {/* Historical, most-recent first (API returns them in
                that order via supersededAt DESC). */}
            {field &&
              entries.map((e, idx) => (
                <TextHistoryRow
                  key={`${e.supersededAt}-${idx}`}
                  field={field}
                  label={`Version ${entries.length - idx}`}
                  sub={`Active ${formatFullDate(e.capturedAt)} → ${formatFullDate(
                    e.supersededAt
                  )}`}
                  value={e.value}
                  variant="history"
                />
              ))}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}

function TextHistoryRow({
  field,
  label,
  sub,
  value,
  variant,
}: {
  field: "title" | "description" | "tags" | "privacy"
  label: string
  sub?: string
  value: unknown
  variant: "current" | "history"
}) {
  return (
    <div className="border border-border p-3 space-y-2">
      <div className="flex items-baseline justify-between gap-3">
        <div
          className={cn(
            "text-[10px] uppercase tracking-wider font-semibold",
            variant === "current"
              ? "text-foreground"
              : "text-muted-foreground"
          )}
        >
          {label}
        </div>
        {sub && (
          <div className="text-[11px] text-muted-foreground font-mono tabular-nums shrink-0">
            {sub}
          </div>
        )}
      </div>
      <TextHistoryValue field={field} value={value} />
    </div>
  )
}

function TextHistoryValue({
  field,
  value,
}: {
  field: "title" | "description" | "tags" | "privacy"
  value: unknown
}) {
  // Empty / missing values get a placeholder so the user can tell we
  // looked vs the row is broken. (We never store "(none)" as a real
  // value, so collision-by-string is fine.)
  if (value == null || value === "") {
    return (
      <div className="text-xs text-muted-foreground italic">(empty)</div>
    )
  }
  if (field === "tags") {
    const tags = Array.isArray(value) ? (value as string[]) : []
    if (tags.length === 0) {
      return (
        <div className="text-xs text-muted-foreground italic">(no tags)</div>
      )
    }
    return (
      <div className="flex flex-wrap gap-1.5">
        {tags.map((t) => (
          <span
            key={t}
            className="text-xs font-semibold border border-border px-2 py-0.5 text-neutral-300"
          >
            {t}
          </span>
        ))}
      </div>
    )
  }
  if (field === "privacy") {
    const v = String(value)
    return (
      <div className="text-sm font-semibold text-neutral-200 capitalize">
        {v}
      </div>
    )
  }
  // title + description: same render - text, descriptions can be huge,
  // so preserve newlines and let the surrounding scroll container do
  // the work.
  return (
    <div className="text-sm whitespace-pre-wrap break-words text-neutral-200 leading-relaxed">
      {String(value)}
    </div>
  )
}
