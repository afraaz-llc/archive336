import type { ReactNode } from "react"
import { Heart, Pencil, Pin, ThumbsUp, Trash2 } from "lucide-react"
import { badgeVariants } from "@/components/ui/badge"
import { formatFullDate, formatRelativeDate } from "@/lib/format"
import { cn } from "@/lib/utils"

/** One archived comment as the API returns it.
 *
 *  channelId, channelName and videoTitle are only sent by the
 *  cross-channel search - the per-channel endpoints already know which
 *  channel the caller is looking at and resolve titles client-side. */
export type ApiComment = {
  id: string
  parentCommentId: string | null
  channelId?: string
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
  channelName?: string
  videoTitle?: string
}

/** The frame a run of CommentRows sits in: one border, hairlines between
 *  rows. Rows used to be separate boxes with a gap between each, which
 *  spent more of the screen on gutters than on comments. */
export function CommentList({ children }: { children: ReactNode }) {
  return (
    <div className="border border-border divide-y divide-border">{children}</div>
  )
}

/**
 * A single comment in a results list, two lines tall when the comment
 * is one line long.
 *
 * Line one is everything about the comment - who, when, where, likes -
 * with the video title right-aligned so it can truncate before anything
 * else does. Line two is the comment.
 *
 * The channel gets a bordered tag and the likes a hairline rule rather
 * than both being run together behind punctuation: a slash or a dot is
 * a character a real channel name or video title can contain, so it
 * cannot be trusted to show where one field ends. A drawn border can.
 * The tag only appears when the list actually mixes channels.
 *
 * Deleted comments are dimmed rather than hidden: keeping what YouTube
 * dropped is the whole point of archiving them.
 *
 * Spans rather than divs throughout: a button may only hold phrasing
 * content.
 */
export function CommentRow({
  comment,
  videoTitle,
  channelName,
  onClick,
}: {
  comment: ApiComment
  videoTitle?: string
  channelName?: string
  onClick: () => void
}) {
  const isDeleted = !!comment.deletedAt
  const where = videoTitle || `Video ${comment.videoId}`

  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "block w-full text-left px-4 py-2.5 cursor-pointer hover:bg-accent",
        isDeleted && "opacity-75"
      )}
    >
      <span className="flex items-center gap-2 text-xs">
        <span className="shrink-0 font-semibold text-foreground">
          {comment.author}
        </span>
        {comment.isByUploader && (
          <span
            className={cn(
              badgeVariants({ variant: "success" }),
              "shrink-0 text-[10px] px-1.5 py-0"
            )}
          >
            Uploader
          </span>
        )}
        {comment.isPinned && (
          <span className="shrink-0 text-muted-foreground" title="Pinned">
            <Pin className="size-3" />
          </span>
        )}
        {comment.viewerRatingLike && (
          <span
            className="shrink-0 text-muted-foreground"
            title="Hearted by uploader"
          >
            <Heart className="size-3" />
          </span>
        )}
        {comment.isEdited && (
          <span className="shrink-0 text-muted-foreground" title="Edited">
            <Pencil className="size-3" />
          </span>
        )}
        {comment.publishedAt && (
          <span
            className="shrink-0 font-mono tabular-nums text-muted-foreground"
            title={formatFullDate(comment.publishedAt)}
          >
            {formatRelativeDate(comment.publishedAt)}
          </span>
        )}
        {isDeleted && (
          <span
            className="shrink-0 inline-flex items-center gap-1 font-mono tabular-nums text-amber-400"
            title={formatFullDate(comment.deletedAt!)}
          >
            <Trash2 className="size-3" />
            Deleted {formatRelativeDate(comment.deletedAt!)}
          </span>
        )}
        <span className="ml-auto flex items-center gap-3 min-w-0">
          {channelName && (
            <span className="shrink-0 max-w-[12rem] truncate border border-border px-1.5 py-0.5 text-[10px] leading-none font-semibold text-foreground/80">
              {channelName}
            </span>
          )}
          <span className="min-w-0 truncate text-muted-foreground" title={where}>
            {where}
          </span>
          <span className="shrink-0 inline-flex items-center gap-1.5 border-l border-border pl-3 font-mono tabular-nums text-muted-foreground">
            <ThumbsUp className="size-3" />
            {comment.likeCount.toLocaleString()}
          </span>
        </span>
      </span>

      <span className="block mt-1 text-sm leading-snug whitespace-pre-wrap break-words text-neutral-200">
        {comment.text}
      </span>
    </button>
  )
}
