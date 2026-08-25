import * as React from "react"
import { useToast } from "./ui/toast"

export type ParsedChannelUrl = {
  /** Stable identifier — for now, same as handle. Backend will swap to UC… later. */
  id: string
  /** Display handle (e.g. "@veritasium" or a UC channel id). */
  handle: string
}

type Props = {
  // Promise return so the form can show a loading state while the
  // backend resolves the channel + fetches initial metadata (which
  // takes a few seconds). Caller doesn't have to return a Promise —
  // we await whatever it returns, sync values resolve immediately.
  onAdd?: (parsed: ParsedChannelUrl) => void | Promise<void>
}

export function AddChannelForm({ onAdd }: Props) {
  const [url, setUrl] = React.useState("")
  const [busy, setBusy] = React.useState(false)
  const { toast } = useToast()

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (busy) return
    const trimmed = url.trim()
    if (!trimmed) return
    const parsed = parseYouTubeChannelUrl(trimmed)
    if (!parsed) {
      toast({
        title: "That doesn't look like a YouTube channel",
        description: "Try a channel name, @handle, or URL.",
        variant: "error",
      })
      return
    }
    setBusy(true)
    try {
      await onAdd?.(parsed)
      setUrl("")
    } finally {
      setBusy(false)
    }
  }

  // Paste-URL flow: submits to POST /api/youtube/channels/track,
  // which resolves the URL/handle to a UC channel id, fetches the
  // channel's public metadata, and creates the legacy UserChannel +
  // new-model Channel + UserChannelSubscription rows. Public-tier
  // access only (members-only + private remain hidden until the
  // owner separately authenticates).
  return (
    <form onSubmit={submit} className="relative">
      <input
        type="text"
        value={url}
        onChange={(e) => setUrl(e.target.value)}
        placeholder="YouTube channel name or URL"
        autoComplete="off"
        spellCheck={false}
        disabled={busy}
        className="block w-full h-14 bg-transparent border-2 border-border px-4 pr-32 text-foreground text-base font-semibold placeholder:text-muted-foreground placeholder:font-semibold outline-none focus:border-foreground disabled:opacity-60"
      />
      <button
        type="submit"
        disabled={busy}
        className="absolute right-3 top-1/2 -translate-y-1/2 text-[10px] uppercase tracking-wider font-bold border border-border px-3 py-1.5 cursor-pointer hover:border-foreground disabled:cursor-wait disabled:opacity-60"
      >
        {busy ? "Tracking…" : "Track"}
      </button>
    </form>
  )
}

/**
 * Pull a channel handle out of any of YouTube's URL shapes.
 * Returns null if the input doesn't look like a channel URL.
 */
export function parseYouTubeChannelUrl(input: string): ParsedChannelUrl | null {
  const s = input.trim()

  // Bare @handle
  const bare = s.match(/^@([\w\-.]+)$/)
  if (bare) return makeHandle(bare[1])

  // youtube.com/@handle (with optional protocol/www/m, optional trailing path)
  const at = s.match(/youtube\.com\/@([\w\-.]+)/)
  if (at) return makeHandle(at[1])

  // youtube.com/channel/UCxxxx — canonical channel id
  const channelId = s.match(/youtube\.com\/channel\/(UC[\w\-]+)/)
  if (channelId) {
    const id = channelId[1]
    return { id, handle: id }
  }

  // youtube.com/c/customname — legacy custom URL
  const c = s.match(/youtube\.com\/c\/([\w\-.]+)/)
  if (c) return makeHandle(c[1])

  // youtube.com/user/oldname — legacy username URL
  const user = s.match(/youtube\.com\/user\/([\w\-.]+)/)
  if (user) return makeHandle(user[1])

  // Bare UC id. Checked before the bare-name rule below, which would
  // otherwise swallow it and ask YouTube for "@UCxxx…".
  if (/^UC[\w-]{20,}$/.test(s)) return { id: s, handle: s }

  // A bare name, no @ and no URL — "MrBeast". This is what most people
  // actually type, and the server already handles it: anything without a
  // UC or @ prefix is looked up as youtube.com/@<name> (verified against
  // production - "MrBeast" and "@MrBeast" resolve to the same channel id).
  // Rejecting it here was the frontend refusing input the backend could
  // have answered.
  //
  // Note this is matched as a HANDLE, not a display name: YouTube has no
  // lookup from "the channel called X" to a channel, so a name that isn't
  // also the handle won't resolve, and the server returns a plain 400.
  // Handles are 3-30 chars of letters, digits, dots, hyphens, underscores.
  if (/^[\w\-.]{3,30}$/.test(s)) return makeHandle(s)

  return null
}

function makeHandle(name: string): ParsedChannelUrl {
  const handle = `@${name}`
  return { id: handle, handle }
}
