import * as React from "react"
import { cn } from "@/lib/utils"

/**
 * A channel's profile picture, with a deterministic fallback.
 *
 * The avatar url can break while the archived image itself is perfectly
 * intact: a stale storage key, an expired signature, a channel that moved.
 * A bare <img> in a rounded bordered box collapses to an empty circle when
 * that happens, which looks exactly like a channel that never had a picture.
 * That ambiguity is the bug - a storage fault and a healthy channel were
 * indistinguishable on screen.
 *
 * So a failed load falls back to the channel's initial in the same box. It
 * reads as deliberate rather than broken, which is what we want: system
 * health is our problem to fix, not something to alarm users about.
 *
 * The failure is keyed to the url that produced it rather than a boolean, so
 * a refreshed url is retried on its own merits and switching channels never
 * shows a stale failure.
 */
export function ChannelAvatar({
  url,
  name,
  size,
  textClassName,
}: {
  url: string | null | undefined
  /** Channel name or handle. Only the first letter is used. */
  name: string
  /** Tailwind size class, e.g. "size-14". Callers differ deliberately. */
  size: string
  /** Tailwind text-size class for the fallback initial. */
  textClassName: string
}) {
  const [failedUrl, setFailedUrl] = React.useState<string | null>(null)
  const show = Boolean(url) && failedUrl !== url

  if (show) {
    return (
      <img
        draggable={false}
        src={url as string}
        alt=""
        referrerPolicy="no-referrer"
        onError={() => setFailedUrl(url as string)}
        className={cn(
          size,
          "rounded-full border border-border shrink-0 object-cover"
        )}
      />
    )
  }

  const initial = name.trim().replace(/^@/, "").charAt(0).toUpperCase()
  return (
    <div
      aria-hidden
      className={cn(
        size,
        textClassName,
        "rounded-full border border-border shrink-0 bg-muted flex items-center justify-center font-bold text-muted-foreground"
      )}
    >
      {initial}
    </div>
  )
}
