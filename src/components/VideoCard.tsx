import { Check } from "lucide-react"
import type {
  ChannelArchiveSettings,
  Video,
  VideoCardMetaField,
} from "@/lib/types"
import { Badge } from "./ui/badge"
import { Progress } from "./ui/progress"
import {
  formatBytes,
  formatDuration,
  formatFullDate,
} from "@/lib/format"
import { cn } from "@/lib/utils"

export type VideoCardVariant = "grid" | "list"

type Props = {
  video: Video
  onClick?: (v: Video) => void
  settings: ChannelArchiveSettings
  selected?: boolean
  onToggleSelect?: (v: Video, shiftKey: boolean) => void
  variant?: VideoCardVariant
  // When false, syncing-state videos render a "Worker app inactive"
  // banner instead of the progress bar so the user understands why
  // the job is sitting at 0%.
  workerActive?: boolean
  // Channel name, for the cross-channel library only. On a channel's
  // own page the channel is the context and repeating it on every row
  // would be noise.
  channelLabel?: string
}

export function VideoCard({
  video,
  onClick,
  settings,
  selected = false,
  onToggleSelect,
  variant = "grid",
  workerActive = true,
  channelLabel,
}: Props) {
  const colors = settings.useStatusColorBorder ? statusColors(video) : null
  const borderClass = colors?.border ?? "border-border"

  const activate = () => onClick?.(video)
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault()
      activate()
    }
  }

  const checkbox = onToggleSelect && (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation()
        onToggleSelect(video, e.shiftKey)
      }}
      aria-label={selected ? "Deselect video" : "Select video"}
      aria-pressed={selected}
      className={cn(
        "flex items-center justify-center border-2 cursor-pointer shrink-0",
        colors?.border ?? "border-white",
        variant === "grid"
          ? "size-6 absolute top-2 right-2 z-10"
          : "size-5",
        selected
          ? colors?.bg ?? "bg-white"
          : cn(
              "opacity-0 group-hover:opacity-100",
              variant === "grid" ? "bg-black/60" : "bg-transparent"
            )
      )}
    >
      {selected && (
        <Check
          className={variant === "grid" ? "size-4 text-black" : "size-3 text-black"}
          strokeWidth={3}
        />
      )}
    </button>
  )

  // ---- LIST VARIANT ---------------------------------------------------------
  if (variant === "list") {
    return (
      <div
        role="button"
        tabIndex={0}
        onClick={activate}
        onKeyDown={handleKeyDown}
        className={cn(
          "group relative flex items-center gap-3 px-3 py-2 border cursor-pointer outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-offset-2 focus-visible:ring-offset-background",
          selected
            ? cn("bg-accent/40", colors?.border ?? "border-white")
            : borderClass
        )}
      >
        {checkbox}

        {/* Title */}
        <div className="min-w-0 flex-1 truncate">
          <span className="text-sm font-medium">{video.title}</span>
          {channelLabel && (
            <span className="ml-2 text-xs text-muted-foreground">
              {channelLabel}
            </span>
          )}
        </div>

        {/* Meta line */}
        {settings.cardMetaFields.length > 0 && (
          <div className="shrink-0">
            <MetaLine video={video} fields={settings.cardMetaFields} />
          </div>
        )}

        {/* Status badges */}
        {settings.showStatusBadges && (
          <div className="shrink-0 flex items-center gap-1.5">
            <VisibilityBadge video={video} />
            <SyncBadge video={video} />
          </div>
        )}

        {/* Sync progress bar along the bottom of the row. When the
            worker is offline the row gets an amber underline instead
            of an empty progress bar - clearer signal that nothing's
            actually happening yet. */}
        {video.status === "syncing" && (
          workerActive ? (
            <Progress
              value={video.syncProgress ?? 0}
              className="absolute inset-x-0 bottom-0 h-[2px]"
            />
          ) : (
            <div className="absolute inset-x-0 bottom-0 h-[2px] bg-amber-500/60" />
          )
        )}
      </div>
    )
  }

  // ---- GRID VARIANT ---------------------------------------------------------
  // Treat the thumbnail as "absent" when either the toggle is off or there's
  // no URL stored. Per the server-side archive model, the URL persists even
  // when the user toggles thumbnails off — we just hide it visually here.
  const hasThumbnail = settings.saveThumbnail && !!video.thumbnailUrl
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={activate}
      onKeyDown={handleKeyDown}
      className="group flex flex-col text-left cursor-pointer outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-offset-2 focus-visible:ring-offset-background"
    >
      <div
        className={cn(
          "relative aspect-video overflow-hidden border bg-muted",
          selected &&
            cn("outline outline-2", colors?.outline ?? "outline-white"),
          borderClass
        )}
      >
        {hasThumbnail ? (
          <img
            draggable={false}
            src={video.thumbnailUrl}
            alt=""
            referrerPolicy="no-referrer"
            className="size-full object-cover"
            loading="lazy"
          />
        ) : (
          // No-thumbnail layout: title + meta fill the box where the image
          // would have been. We pin the content to the bottom so the
          // top of the card is reserved for whatever overlay badges
          // happen to be there (which can wrap to multiple rows).
          // Right padding clears the hover checkbox in the top-right.
          <div className="absolute inset-x-0 bottom-0 flex flex-col gap-1 pl-3 pr-10 pb-3 pt-2">
            <div className="font-semibold text-sm line-clamp-2 leading-snug">
              {video.title}
            </div>
            {channelLabel && (
              <div className="text-xs text-muted-foreground truncate">
                {channelLabel}
              </div>
            )}
            {settings.cardMetaFields.length > 0 && (
              <MetaLine video={video} fields={settings.cardMetaFields} />
            )}
          </div>
        )}

        {checkbox}

        {/* Duration pill — only over the thumbnail; when there's no
            thumbnail, duration is in the inline MetaLine if the user wants
            it as a card field. */}
        {hasThumbnail && (
          <div className="absolute bottom-2 right-2 bg-black/80 px-1.5 py-0.5 text-[11px] font-mono text-white tabular-nums">
            {formatDuration(video.durationSec)}
          </div>
        )}

        {/* Sync progress (or 'Worker app inactive' overlay when the
            user's desktop worker isn't running yet) */}
        {video.status === "syncing" && (
          <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/90 via-black/60 to-transparent p-3">
            {workerActive ? (
              <>
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
              </>
            ) : (
              <div className="text-xs text-amber-200 font-semibold">
                Worker app inactive — waiting…
              </div>
            )}
          </div>
        )}

        {/* Status badges */}
        {settings.showStatusBadges && (
          <div className="absolute top-2 left-2 flex flex-wrap gap-1.5 max-w-[calc(100%-5rem)]">
            <VisibilityBadge video={video} />
            <SyncBadge video={video} />
          </div>
        )}
      </div>

      {/* External title + meta — only when there's a thumbnail. When the
          thumbnail is off, those moved inside the box. */}
      {hasThumbnail && (
        <div className="mt-3 space-y-1 min-w-0">
          <div
            className="font-medium text-sm line-clamp-2 leading-snug"
          >
            {video.title}
          </div>
          {channelLabel && (
            <div className="text-xs text-muted-foreground truncate">
              {channelLabel}
            </div>
          )}
          {settings.cardMetaFields.length > 0 && (
            <MetaLine video={video} fields={settings.cardMetaFields} />
          )}
        </div>
      )}
    </div>
  )
}

function MetaLine({
  video,
  fields,
}: {
  video: Video
  fields: VideoCardMetaField[]
}) {
  const parts: { key: string; node: React.ReactNode }[] = []

  for (const f of fields) {
    switch (f) {
      case "uploadDate":
        parts.push({
          key: "uploadDate",
          node: <span>{formatFullDate(video.uploadDate)}</span>,
        })
        break
      case "duration":
        parts.push({
          key: "duration",
          node: (
            <span className="font-mono tabular-nums">
              {formatDuration(video.durationSec)}
            </span>
          ),
        })
        break
      case "fileSize":
        // Only when we actually hold bytes. The list endpoint sends 0 for
        // anything the caller has not archived, so a `!= null` test let
        // every un-synced card print "0 B" - a measurement of nothing,
        // sitting where a real size goes. Aggregate stats still show 0 B,
        // because there the label gives the zero meaning.
        if (video.fileSizeBytes) {
          parts.push({
            key: "fileSize",
            node: (
              <span className="font-mono tabular-nums">
                {formatBytes(video.fileSizeBytes)}
              </span>
            ),
          })
        }
        break
      case "type":
        parts.push({
          key: "type",
          node: <span className="capitalize">{video.type}</span>,
        })
        break
    }
  }

  if (parts.length === 0) return null

  return (
    <div className="text-xs text-muted-foreground flex flex-wrap items-center gap-x-2 gap-y-0.5">
      {parts.map((p, i) => (
        <span key={p.key} className="inline-flex items-center gap-2">
          {i > 0 && <span className="opacity-50">·</span>}
          {p.node}
        </span>
      ))}
    </div>
  )
}

// Two backend writers disagree on the members-only privacy value: one writes
// "members", the other "members_only". Recognizing only one spelling meant a
// members_only row rendered no badge and no border color at all. The backend
// is being normalized separately, but tolerating both spellings here keeps
// rows already written with the old value readable. Typed as string[] on
// purpose - "members_only" is outside the declared VideoPrivacy union, which
// is exactly why it has to be matched before the switches below.
const MEMBERS_SPELLINGS: string[] = ["members", "members_only"]

type StatusColors = { border: string; outline: string; bg: string }

// Shared so both spellings resolve to the identical object, and so the
// border can never drift from the Members badge.
const MEMBERS_COLORS: StatusColors = {
  border: "border-emerald-400",
  outline: "outline-emerald-400",
  bg: "bg-emerald-400",
}

function statusColors(v: Video): StatusColors | null {
  // Border/outline/bg classes for the video's visibility state. Returns null
  // for the neutral default (public, no special color) — callers fall back
  // to whites/border-border on null. Each color matches its badge so the
  // border, selection outline ring, and selection checkbox all read as
  // one signal.
  if (v.status === "deleted_on_youtube") {
    return {
      border: "border-blue-400",
      outline: "outline-blue-400",
      bg: "bg-blue-400",
    }
  }
  if (MEMBERS_SPELLINGS.includes(v.privacy)) {
    return MEMBERS_COLORS
  }
  switch (v.privacy) {
    case "unlisted":
      return {
        border: "border-amber-400",
        outline: "outline-amber-400",
        bg: "bg-amber-400",
      }
    case "private":
      return {
        border: "border-red-500",
        outline: "outline-red-500",
        bg: "bg-red-500",
      }
    // Handled above; kept so the switch stays exhaustive over VideoPrivacy.
    case "members":
      return MEMBERS_COLORS
  }
  return null
}

/**
 * Single badge for where this video is on YouTube. One word for each state:
 * Public, Unlisted, Private, Members, Unavailable.
 *
 * "Unavailable" rather than "Deleted" because we cannot tell who or what
 * removed the video: takedowns, TOS removals, terminated accounts, region
 * blocks, age gates and bot-checks against our scraper all land in the same
 * state. All we know is that we could not see it when we last looked.
 */
function VisibilityBadge({ video }: { video: Video }) {
  if (video.status === "deleted_on_youtube") {
    return <Badge variant="deleted">Unavailable</Badge>
  }
  if (MEMBERS_SPELLINGS.includes(video.privacy)) {
    return <Badge variant="members">Members</Badge>
  }
  switch (video.privacy) {
    case "public":
      return <Badge variant="outline">Public</Badge>
    case "unlisted":
      return <Badge variant="warning">Unlisted</Badge>
    case "private":
      return <Badge variant="private">Private</Badge>
    // Handled above; kept so the switch stays exhaustive over VideoPrivacy.
    case "members":
      return <Badge variant="members">Members</Badge>
  }
}

/**
 * Single badge for the local sync state. "Synced" is the default — no badge
 * means we have the file locally. We key off localPath rather than status
 * so deleted-on-youtube videos still correctly read as "Not synced" when
 * we never grabbed a local copy.
 */
function SyncBadge({ video }: { video: Video }) {
  if (video.status === "syncing") {
    return <Badge variant="outline">Syncing</Badge>
  }
  if (video.status === "failed") {
    return <Badge variant="destructive">Failed</Badge>
  }
  if (video.localPath) return null
  return <Badge variant="destructive">Not synced</Badge>
}
