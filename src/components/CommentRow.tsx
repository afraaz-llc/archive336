import { Heart, MessageSquare, Pencil, Pin, ThumbsUp, Trash2, Tv } from "lucide-react"
import { Badge } from "@/components/ui/badge"
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

/**
 * A single comment in a results list.
 *
 * Shared by the channel comments page and the Comments scope on the
 * YouTube page so a comment reads the same wherever it is found. The
 * channel line only appears when the list actually mixes channels.
 *
 * Deleted comments are dimmed rather than hidden: keeping what YouTube
 * dropped is the whole point of archiving them.
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
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "block w-full text-left border border-border p-4 space-y-2 cursor-pointer hover:bg-accent",
        isDeleted && "opacity-75"
      )}
    >
      <div className="flex items-center gap-2 flex-wrap text-xs">
        <span className="font-semibold text-foreground">{comment.author}</span>
        {comment.isByUploader && (
          <Badge variant="success" className="text-[10px] px-1.5 py-0">
            Uploader
          </Badge>
        )}
        {comment.isPinned && (
          <span className="text-muted-foreground" title="Pinned">
            <Pin className="size-3" />
          </span>
        )}
        {comment.viewerRatingLike && (
          <span className="text-muted-foreground" title="Hearted by uploader">
            <Heart className="size-3" />
          </span>
        )}
        {comment.isEdited && (
          <span className="text-muted-foreground" title="Edited">
            <Pencil className="size-3" />
          </span>
        )}
        {comment.publishedAt && (
          <span
            className="text-muted-foreground font-mono tabular-nums"
            title={formatFullDate(comment.publishedAt)}
          >
            {formatRelativeDate(comment.publishedAt)}
          </span>
        )}
        <span className="ml-auto inline-flex items-center gap-1 text-muted-foreground font-mono tabular-nums">
          <ThumbsUp className="size-3" />
          {comment.likeCount.toLocaleString()}
        </span>
      </div>

      <div className="text-sm whitespace-pre-wrap break-words text-neutral-200 leading-relaxed">
        {comment.text}
      </div>

      <div className="flex items-center justify-between gap-3 pt-1 text-[11px] text-muted-foreground">
        <div className="inline-flex items-center gap-1 truncate">
          {channelName && (
            <>
              <Tv className="size-3 shrink-0" />
              <span className="truncate">{channelName}</span>
              <span className="text-border px-0.5">/</span>
            </>
          )}
          <MessageSquare className="size-3 shrink-0" />
          <span className="truncate">
            {videoTitle || `Video ${comment.videoId}`}
          </span>
        </div>
        {isDeleted && (
          <div
            className="inline-flex items-center gap-1 text-amber-400 font-mono tabular-nums shrink-0"
            title={formatFullDate(comment.deletedAt!)}
          >
            <Trash2 className="size-3" />
            Deleted {formatRelativeDate(comment.deletedAt!)}
          </div>
        )}
      </div>
    </button>
  )
}
