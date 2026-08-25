import * as React from "react"
import { AlertCircle, RefreshCw } from "lucide-react"
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetBody,
  SheetFooter,
} from "./ui/sheet"
import { Button } from "./ui/button"
import { Switch } from "./ui/switch"
import { useWorkerStatus } from "@/lib/workerStatus"
import type { Channel, ChannelArchiveSettings, Video } from "@/lib/types"
import {
  estimateDownloadCostUsd,
  estimateGbPerHour,
  isQualityOutdated,
} from "@/lib/estimates"
import { formatBytes } from "@/lib/format"
import { usePrices } from "@/lib/pricing"

export type SyncMetadataFields = {
  saveThumbnail: boolean
  saveViewCount: boolean
  saveDescription: boolean
  saveTags: boolean
}

export type SyncOptions = {
  /** Unified videos toggle: discover new uploads, retry failed jobs,
   *  AND enqueue file downloads for everything in 'discovered' state.
   *  Replaces the older newVideos + files split. New video syncs
   *  capture captions automatically as a side-effect of yt-dlp. */
  videos: boolean
  /** Captions backfill: enqueue captions-kind jobs for every already-
   *  archived video on this channel so we pick up tracks the channel
   *  owner added after the original archive ran. Doesn't touch the
   *  mp4 - just writes new VTTs into R2 and updates the metadata. */
  captions: boolean
  metadata: boolean
  fields: SyncMetadataFields
}

type Props = {
  channel: Channel
  settings: ChannelArchiveSettings
  open: boolean
  onOpenChange: (open: boolean) => void
  onConfirm: (options: SyncOptions) => void
  /** All known videos for the channel - used to count outdated archives
   * so we can show a "X videos will be re-archived (~Y GB)" preview
   * before the user confirms. Cost preview is hidden when this is empty.
   * Named `allVideos` to avoid collision with the videos toggle state
   * variable inside this component. */
  allVideos?: Video[]
}

function fieldsFromSettings(s: ChannelArchiveSettings): SyncMetadataFields {
  return {
    saveThumbnail: s.saveThumbnail,
    saveViewCount: s.saveViewCount,
    saveDescription: s.saveDescription,
    saveTags: s.saveTags,
  }
}

export function SyncPanel({
  channel,
  settings,
  open,
  onOpenChange,
  onConfirm,
  allVideos,
}: Props) {
  // Every scope defaults to whatever this channel is configured to capture
  // automatically, so a manual sync mirrors the automatic one rather than
  // quietly doing something different. Captions used to be hardcoded off,
  // which meant a channel with Captions ON still skipped them here.
  const [videos, setVideos] = React.useState(true)
  const [captions, setCaptions] = React.useState(settings.saveCaptions)
  const [metadata, setMetadata] = React.useState(true)
  const [fields, setFields] = React.useState<SyncMetadataFields>(
    fieldsFromSettings(settings)
  )

  React.useEffect(() => {
    if (!open) return
    setVideos(true)
    setCaptions(settings.saveCaptions)
    setMetadata(true)
    setFields(fieldsFromSettings(settings))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, settings])

  // Captions now nests under Metadata - if the user has Metadata off,
  // they can't see or change the Captions sub-toggle, so we ignore any
  // stale 'captions: true' state for gating purposes.
  const effectiveCaptions = metadata && captions
  const canSync = videos || metadata

  const submit = () => {
    onConfirm({
      videos,
      captions: effectiveCaptions,
      metadata,
      fields,
    })
    onOpenChange(false)
  }

  // Poll worker status while the panel is open and any worker-driven
  // job is selected - both Videos and Captions run via the worker
  // (yt-dlp). Metadata refresh on its own is server-side OAuth and
  // doesn't need the worker.
  const needsWorker = videos || effectiveCaptions
  const worker = useWorkerStatus(open && needsWorker)
  const workerOffline = open && needsWorker && !worker.active

  // Outdated archive preview: count videos whose stamped quality
  // settings don't match the channel's current quality settings.
  // Triggers a "this will re-download X videos at <Y> quality, ~Z GB,
  // ~$W" info row so the user knows the cost before confirming.
  const outdatedVideos = React.useMemo(() => {
    if (!allVideos) return []
    const current = {
      resolution: settings.maxResolution,
      codec: settings.codecPreference,
    }
    return allVideos.filter(
      (v) =>
        v.status === "archived" &&
        isQualityOutdated(current, {
          resolution: v.archivedMaxResolution,
          codec: v.archivedCodecPreference,
        })
    )
  }, [allVideos, settings.maxResolution, settings.codecPreference])

  const prices = usePrices()
  const outdatedEstimate = React.useMemo(() => {
    if (outdatedVideos.length === 0) return null
    const gbPerHour = estimateGbPerHour(
      settings.maxResolution,
      settings.codecPreference
    )
    const totalSeconds = outdatedVideos.reduce(
      (acc, v) => acc + (v.durationSec || 0),
      0
    )
    const totalGb = (gbPerHour * totalSeconds) / 3600
    return {
      count: outdatedVideos.length,
      totalGb,
      costUsd: estimateDownloadCostUsd(totalGb, prices.downloadPerGb),
    }
  }, [
    outdatedVideos,
    settings.maxResolution,
    settings.codecPreference,
    prices.downloadPerGb,
  ])

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent>
        <SheetHeader>
          <SheetTitle>Sync from {channel.name}</SheetTitle>
        </SheetHeader>

        <SheetBody className="space-y-6">
          {workerOffline && (
            <div className="flex items-start gap-3 border border-amber-500/40 bg-amber-500/5 p-3 text-xs">
              <AlertCircle className="size-4 text-amber-400 shrink-0 mt-0.5" />
              <div className="leading-relaxed">
                <div className="font-semibold text-amber-200 mb-0.5">
                  Worker app inactive
                </div>
                <div className="text-amber-200/80">
                  File syncs will queue and start automatically as soon
                  as you launch the worker app.
                </div>
              </div>
            </div>
          )}

          {/* Outdated re-archive cost preview. Only shown when there's
              actually re-work to do AND the Videos toggle is selected -
              if the user opts out of video sync they won't trigger any
              re-downloads anyway, so don't surface the cost. */}
          {videos && outdatedEstimate && (
            <div className="border border-border p-3 space-y-1.5 text-xs leading-relaxed">
              <div className="font-semibold text-foreground">
                {outdatedEstimate.count}{" "}
                {outdatedEstimate.count === 1 ? "video" : "videos"} will be
                re-archived
              </div>
              <div className="text-muted-foreground">
                Quality settings changed since these were archived.
                Re-downloading at {settings.maxResolution} ·{" "}
                {settings.codecPreference === "efficient"
                  ? "AV1/VP9"
                  : "H.264"}{" "}
                will replace the old files in place.
              </div>
              <div className="flex items-center gap-3 pt-1 font-mono tabular-nums">
                <span className="text-foreground font-semibold">
                  ~{formatBytes(outdatedEstimate.totalGb * 1_000_000_000)}
                </span>
                <span className="text-muted-foreground">
                  ~${outdatedEstimate.costUsd.toFixed(2)} bandwidth
                </span>
              </div>
            </div>
          )}
          <Section label="Scope">
            <ToggleRow
              label="Videos"
              checked={videos}
              onChange={setVideos}
            />
            <ToggleRow
              label="Metadata"
              checked={metadata}
              onChange={setMetadata}
            />
            {metadata && (
              <div className="pl-4 ml-2 space-y-3 border-l-2 border-white/25">
                <ToggleRow
                  label="Thumbnail"
                  checked={fields.saveThumbnail}
                  onChange={(v) =>
                    setFields((f) => ({ ...f, saveThumbnail: v }))
                  }
                />
                <ToggleRow
                  label="View count"
                  checked={fields.saveViewCount}
                  onChange={(v) =>
                    setFields((f) => ({ ...f, saveViewCount: v }))
                  }
                />
                <ToggleRow
                  label="Description"
                  checked={fields.saveDescription}
                  onChange={(v) =>
                    setFields((f) => ({ ...f, saveDescription: v }))
                  }
                />
                <ToggleRow
                  label="Tags"
                  checked={fields.saveTags}
                  onChange={(v) => setFields((f) => ({ ...f, saveTags: v }))}
                />
                {/* Captions sit under Metadata as a sub-toggle for
                    visual grouping, but actually run through the
                    worker (/sync-captions) rather than the fast
                    server-side OAuth /sync-metadata path. Only fires
                    when Metadata is on AND this sub-toggle is on. */}
                <ToggleRow
                  label="Captions"
                  checked={captions}
                  onChange={setCaptions}
                />
              </div>
            )}
          </Section>
        </SheetBody>

        <SheetFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={!canSync}>
            <RefreshCw />
            Sync
          </Button>
        </SheetFooter>
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
