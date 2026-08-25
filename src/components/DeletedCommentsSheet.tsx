import * as React from "react"
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetBody,
  SheetFooter,
} from "./ui/sheet"
import { Button } from "./ui/button"
import { Badge } from "./ui/badge"
import { Heart, MessageSquare, Pencil, Pin, ThumbsUp, Trash2 } from "lucide-react"
import { useNavigate, useLocation } from "react-router-dom"
import { formatFullDate, formatRelativeDate } from "@/lib/format"

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

type VideoLookup = (videoId: string) => { title: string } | null

type Props = {
  channelId: string
  open: boolean
  onOpenChange: (v: boolean) => void
  /** Optional lookup so we can render "Comment on <video title>" instead
   * of just an opaque video ID. ChannelDetail passes through its loaded
   * videos array. */
  videoLookup?: VideoLookup
}

const PAGE_SIZE = 50

/**
 * Channel-wide feed of every comment that's been soft-deleted from
 * YouTube since we started archiving.
 *
 * The headline feature of the comments archive - what justifies the
 * higher price tier. Sorted by deletion time, most recent first.
 * Load-more pagination at 50 entries per page.
 */
export function DeletedCommentsSheet({
  channelId,
  open,
  onOpenChange,
  videoLookup,
}: Props) {
  const [pages, setPages] = React.useState<ApiComment[]>([])
  const [total, setTotal] = React.useState(0)
  const [loading, setLoading] = React.useState(false)
  const [loadedAt, setLoadedAt] = React.useState(0)
  const navigate = useNavigate()
  const location = useLocation()

  const loadPage = React.useCallback(
    async (offset: number) => {
      setLoading(true)
      try {
        const params = new URLSearchParams({
          limit: String(PAGE_SIZE),
          offset: String(offset),
        })
        const res = await fetch(
          `/api/youtube/channels/${encodeURIComponent(channelId)}/comments/recently-deleted?${params.toString()}`,
          { credentials: "include" }
        )
        if (!res.ok) return
        const data = (await res.json()) as CommentsResponse
        setTotal(data.total)
        if (offset === 0) {
          setPages(data.comments)
        } else {
          setPages((prev) => [...prev, ...data.comments])
        }
      } finally {
        setLoading(false)
      }
    },
    [channelId]
  )

  // Reload from offset=0 whenever the sheet opens. Cap re-fetch to
  // once per minute to avoid spamming the endpoint when the user
  // opens/closes repeatedly.
  React.useEffect(() => {
    if (!open) return
    if (Date.now() - loadedAt < 60_000 && pages.length > 0) return
    void loadPage(0)
    setLoadedAt(Date.now())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const canLoadMore = pages.length < total

  const onCommentClick = (videoId: string) => {
    // Compose into the channel URL by setting the ?video=<id> param.
    // ChannelDetail's video detail panel picks it up.
    const sp = new URLSearchParams(location.search)
    sp.set("video", videoId)
    navigate({ pathname: location.pathname, search: sp.toString() })
    onOpenChange(false)
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent>
        <SheetHeader>
          <SheetTitle>Deleted comments</SheetTitle>
          {total > 0 && (
            <div className="text-sm text-muted-foreground mt-1 font-mono tabular-nums">
              {total.toLocaleString()} preserved
            </div>
          )}
        </SheetHeader>

        <SheetBody className="space-y-3">
          {pages.length === 0 && !loading && (
            <div className="text-sm text-muted-foreground py-8 leading-relaxed text-center">
              <Trash2 className="mx-auto size-6 mb-3 opacity-50" />
              No comments have been deleted from YouTube yet.
              <div className="text-xs mt-1">
                Whenever a rescan finds a comment that's gone, it'll be
                preserved here.
              </div>
            </div>
          )}

          {pages.map((c) => {
            const vid = videoLookup?.(c.videoId)
            return (
              <button
                key={c.id}
                type="button"
                onClick={() => onCommentClick(c.videoId)}
                className="block w-full text-left border border-border p-3 space-y-1.5 cursor-pointer hover:bg-accent"
              >
                <div className="flex items-center gap-2 flex-wrap text-xs">
                  <span className="font-semibold text-foreground">
                    {c.author}
                  </span>
                  {c.isByUploader && (
                    <Badge variant="success" className="text-[10px] px-1.5 py-0">
                      Uploader
                    </Badge>
                  )}
                  {c.isPinned && (
                    <span className="text-muted-foreground" title="Pinned">
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
                    <span className="text-muted-foreground" title="Edited">
                      <Pencil className="size-3" />
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

                <div className="flex items-center justify-between gap-3 pt-1 text-[11px] text-muted-foreground">
                  <div className="inline-flex items-center gap-1 truncate">
                    <MessageSquare className="size-3 shrink-0" />
                    <span className="truncate">
                      {vid?.title ?? `Video ${c.videoId}`}
                    </span>
                  </div>
                  <div
                    className="inline-flex items-center gap-1 text-amber-400 font-mono tabular-nums shrink-0"
                    title={c.deletedAt ? formatFullDate(c.deletedAt) : undefined}
                  >
                    <Trash2 className="size-3" />
                    {c.deletedAt ? formatRelativeDate(c.deletedAt) : "—"}
                  </div>
                </div>
              </button>
            )
          })}

          {canLoadMore && (
            <Button
              variant="outline"
              className="w-full"
              onClick={() => loadPage(pages.length)}
              disabled={loading}
            >
              {loading ? "Loading…" : `Load more (${(total - pages.length).toLocaleString()} remaining)`}
            </Button>
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
