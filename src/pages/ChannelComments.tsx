import * as React from "react"
import { Link, useLocation, useNavigate, useParams } from "react-router-dom"
import {
  ArrowLeft,
  Heart,
  MessageSquare,
  Pencil,
  Pin,
  Search,
  ThumbsUp,
  Trash2,
  X,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { formatFullDate, formatRelativeDate } from "@/lib/format"
import { normalizeChannelSettings } from "@/lib/mockData"
import type { Channel, Video } from "@/lib/types"
import { cn } from "@/lib/utils"

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

type SortMode = "new" | "top" | "deleted"

const PAGE_SIZE = 50

/**
 * Full search + filter surface for a channel's archived comments.
 *
 * Reachable from the channel detail page (footer link). Lives at
 * /youtube/channel/:channelId/comments. Filters: free-text search,
 * sort mode, only-deleted flag, video filter, min-likes threshold.
 *
 * Clicking a result navigates to the channel detail page with the
 * matching video's detail panel open.
 */
export default function ChannelComments() {
  const { channelId = "" } = useParams<{ channelId: string }>()
  const navigate = useNavigate()
  const location = useLocation()

  const [channel, setChannel] = React.useState<Channel | null>(null)
  const [videos, setVideos] = React.useState<Video[]>([])

  // Filter state
  const [q, setQ] = React.useState("")
  const [debouncedQ, setDebouncedQ] = React.useState("")
  const [sort, setSort] = React.useState<SortMode>("new")
  const [onlyDeleted, setOnlyDeleted] = React.useState(false)
  const [videoId, setVideoId] = React.useState<string>("")
  const [minLikes, setMinLikes] = React.useState<string>("")

  // Results
  const [comments, setComments] = React.useState<ApiComment[]>([])
  const [total, setTotal] = React.useState(0)
  const [loading, setLoading] = React.useState(false)

  // Load channel + videos once for context (title display, video
  // dropdown). Both endpoints exist already on the channel routes.
  React.useEffect(() => {
    let cancelled = false
    fetch(
      `/api/youtube/channels/${encodeURIComponent(channelId)}`,
      { credentials: "include" }
    )
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (!cancelled && d) {
          const ch = d as Channel
          setChannel({
            ...ch,
            settings: normalizeChannelSettings(ch.settings),
          })
        }
      })
      .catch(() => {})
    // Walk the cursor-paginated /videos endpoint until exhausted. We
    // only need the videos for the title-lookup dropdown / display, so
    // we don't bulk-presign thumbnails here.
    void (async () => {
      const collected: Video[] = []
      let cursor: string | null = null
      // eslint-disable-next-line no-constant-condition
      while (true) {
        if (cancelled) return
        const url = new URL(
          `/api/youtube/channels/${encodeURIComponent(channelId)}/videos`,
          window.location.origin
        )
        if (cursor) url.searchParams.set("cursor", cursor)
        url.searchParams.set("limit", "200")
        const r = await fetch(url.toString(), { credentials: "include" })
        if (!r.ok) break
        const body = (await r.json()) as {
          items?: Video[]
          nextCursor?: string | null
        }
        if (!body || !Array.isArray(body.items)) break
        collected.push(...body.items)
        if (!body.nextCursor) break
        cursor = body.nextCursor
      }
      if (!cancelled) setVideos(collected)
    })()
    return () => {
      cancelled = true
    }
  }, [channelId])

  // Debounce search input
  React.useEffect(() => {
    const t = window.setTimeout(() => setDebouncedQ(q), 250)
    return () => window.clearTimeout(t)
  }, [q])

  const fetchResults = React.useCallback(
    async (offset: number) => {
      setLoading(true)
      try {
        const params = new URLSearchParams({
          limit: String(PAGE_SIZE),
          offset: String(offset),
          sort,
        })
        if (debouncedQ) params.set("q", debouncedQ)
        if (videoId) params.set("video_id", videoId)
        if (onlyDeleted) params.set("only_deleted", "true")
        if (minLikes) {
          const n = Number(minLikes)
          if (!Number.isNaN(n) && n > 0) {
            params.set("min_likes", String(Math.floor(n)))
          }
        }
        const res = await fetch(
          `/api/youtube/channels/${encodeURIComponent(channelId)}/comments/search?${params.toString()}`,
          { credentials: "include" }
        )
        if (!res.ok) return
        const data = (await res.json()) as CommentsResponse
        setTotal(data.total)
        if (offset === 0) {
          setComments(data.comments)
        } else {
          setComments((prev) => [...prev, ...data.comments])
        }
      } finally {
        setLoading(false)
      }
    },
    [channelId, debouncedQ, sort, onlyDeleted, videoId, minLikes]
  )

  // Re-fetch from offset=0 whenever any filter changes.
  React.useEffect(() => {
    void fetchResults(0)
  }, [fetchResults])

  const videoTitleById = React.useMemo(() => {
    const m: Record<string, string> = {}
    for (const v of videos) m[v.id] = v.title
    return m
  }, [videos])

  const canLoadMore = comments.length < total

  const openVideo = (vid: string) => {
    const sp = new URLSearchParams(location.search)
    sp.set("video", vid)
    navigate({
      pathname: `/youtube/channel/${encodeURIComponent(channelId)}`,
      search: sp.toString(),
    })
  }

  const clearAll = () => {
    setQ("")
    setSort("new")
    setOnlyDeleted(false)
    setVideoId("")
    setMinLikes("")
  }

  return (
    <div className="min-h-full">
      <div className="border-b border-border bg-card/30">
        <div className="p-8 max-w-4xl mx-auto">
          <div className="flex items-center justify-between gap-3 mb-4">
            <Button
              variant="ghost"
              size="sm"
              asChild
              className="-ml-3 text-muted-foreground"
            >
              <Link to={`/youtube/channel/${encodeURIComponent(channelId)}`}>
                <ArrowLeft />
                Back to {channel?.name ?? "channel"}
              </Link>
            </Button>
          </div>
          <h1 className="text-2xl font-extrabold tracking-tight">Comments</h1>
          {channel?.name && (
            <p className="text-sm text-muted-foreground mt-1">
              Archived comment history for {channel.name}
            </p>
          )}
        </div>
      </div>

      <div className="p-8 max-w-4xl mx-auto space-y-6">
        {/* Filter row */}
        <div className="border border-border p-4 space-y-3">
          <div className="grid grid-cols-1 md:grid-cols-12 gap-3 items-end">
            <div className="md:col-span-7">
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-1.5">
                Search
              </div>
              <div className="relative">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 size-4 text-muted-foreground pointer-events-none" />
                <input
                  type="text"
                  placeholder="Search comment text"
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  className="h-9 w-full border border-border bg-transparent pl-9 pr-8 text-sm text-foreground outline-none placeholder:text-muted-foreground focus:border-white focus:bg-white/5"
                />
                {q && (
                  <button
                    type="button"
                    onClick={() => setQ("")}
                    className="absolute right-2 top-1/2 -translate-y-1/2 size-6 flex items-center justify-center text-muted-foreground hover:text-foreground cursor-pointer"
                    aria-label="Clear search"
                  >
                    <X className="size-4" />
                  </button>
                )}
              </div>
            </div>

            <div className="md:col-span-3">
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-1.5">
                Sort
              </div>
              <Select value={sort} onValueChange={(v) => setSort(v as SortMode)}>
                <SelectTrigger className="w-full h-9">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="new">Newest first</SelectItem>
                  <SelectItem value="top">Most liked</SelectItem>
                  <SelectItem value="deleted">Recently deleted</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="md:col-span-2">
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-1.5">
                Min likes
              </div>
              <input
                type="number"
                min={0}
                placeholder="0"
                value={minLikes}
                onChange={(e) => setMinLikes(e.target.value)}
                className="h-9 w-full border border-border bg-transparent px-3 text-sm text-foreground outline-none placeholder:text-muted-foreground focus:border-white focus:bg-white/5 font-mono tabular-nums"
              />
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-12 gap-3 items-end">
            <div className="md:col-span-7">
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-1.5">
                Video
              </div>
              <Select
                value={videoId || "__all__"}
                onValueChange={(v) => setVideoId(v === "__all__" ? "" : v)}
              >
                <SelectTrigger className="w-full h-9">
                  <SelectValue placeholder="All videos" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="__all__">All videos</SelectItem>
                  {videos.map((v) => (
                    <SelectItem key={v.id} value={v.id}>
                      {v.title}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="md:col-span-3 flex items-center justify-between gap-3 h-9">
              <div className="text-sm font-semibold">Only deleted</div>
              <Switch
                checked={onlyDeleted}
                onCheckedChange={setOnlyDeleted}
                aria-label="Only show deleted comments"
              />
            </div>

            <div className="md:col-span-2">
              <Button
                variant="outline"
                className="w-full h-9"
                onClick={clearAll}
                disabled={
                  !q &&
                  sort === "new" &&
                  !onlyDeleted &&
                  !videoId &&
                  !minLikes
                }
              >
                <X />
                Clear
              </Button>
            </div>
          </div>
        </div>

        {/* Results count */}
        <div className="text-sm text-muted-foreground font-mono tabular-nums">
          {loading && comments.length === 0
            ? "Loading…"
            : total === 0
            ? "0 results"
            : `${total.toLocaleString()} ${total === 1 ? "result" : "results"}`}
        </div>

        {/* Results */}
        <div className="space-y-3">
          {comments.length === 0 && !loading && (
            <div className="border border-border p-8 text-center text-sm text-muted-foreground">
              <MessageSquare className="mx-auto size-6 mb-3 opacity-50" />
              No comments match these filters.
              <div className="text-xs mt-1">
                Comments only appear once a rescan has run. If you just enabled
                Comments sync, give it a day.
              </div>
            </div>
          )}

          {comments.map((c) => {
            const isDeleted = !!c.deletedAt
            const videoTitle = videoTitleById[c.videoId]
            return (
              <button
                key={c.id}
                type="button"
                onClick={() => openVideo(c.videoId)}
                className={cn(
                  "block w-full text-left border border-border p-4 space-y-2 cursor-pointer hover:bg-accent",
                  isDeleted && "opacity-75"
                )}
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
                  {c.publishedAt && (
                    <span
                      className="text-muted-foreground font-mono tabular-nums"
                      title={formatFullDate(c.publishedAt)}
                    >
                      {formatRelativeDate(c.publishedAt)}
                    </span>
                  )}
                  <span className="ml-auto inline-flex items-center gap-1 text-muted-foreground font-mono tabular-nums">
                    <ThumbsUp className="size-3" />
                    {c.likeCount.toLocaleString()}
                  </span>
                </div>

                <div className="text-sm whitespace-pre-wrap break-words text-neutral-200 leading-relaxed">
                  {c.text}
                </div>

                <div className="flex items-center justify-between gap-3 pt-1 text-[11px] text-muted-foreground">
                  <div className="inline-flex items-center gap-1 truncate">
                    <MessageSquare className="size-3 shrink-0" />
                    <span className="truncate">
                      {videoTitle ?? `Video ${c.videoId}`}
                    </span>
                  </div>
                  {isDeleted && (
                    <div
                      className="inline-flex items-center gap-1 text-amber-400 font-mono tabular-nums shrink-0"
                      title={formatFullDate(c.deletedAt!)}
                    >
                      <Trash2 className="size-3" />
                      Deleted {formatRelativeDate(c.deletedAt!)}
                    </div>
                  )}
                </div>
              </button>
            )
          })}

          {canLoadMore && (
            <Button
              variant="outline"
              className="w-full"
              onClick={() => fetchResults(comments.length)}
              disabled={loading}
            >
              {loading
                ? "Loading…"
                : `Load more (${(total - comments.length).toLocaleString()} remaining)`}
            </Button>
          )}
        </div>
      </div>
    </div>
  )
}
