import * as React from "react"
import { Code2, ExternalLink, PlaySquare } from "lucide-react"
import { Button } from "@/components/ui/button"
import { useToast } from "@/components/ui/toast"
import { formatRelativeDate } from "@/lib/format"
import { useDocumentTitle } from "@/lib/useDocumentTitle"

/**
 * Support - the room in the clubhouse where the maintainer is visible.
 *
 * The landing page opens with "Welcome all Preservation Pirates! Make
 * yourselves at home", and then the product had no human being anywhere
 * in it. For a backup tool that gap matters more than it would
 * elsewhere: working correctly looks exactly like being abandoned, so
 * months of silence is the normal experience, and "is anyone still
 * running this?" is the question every user eventually asks about
 * something holding videos they cannot replace.
 *
 * So this page exists to answer that continuously, whether or not
 * anyone writes in. The changelog is the load-bearing part - evidence
 * of a person shipping, visible on every visit, costing the reader
 * nothing.
 */

// The devlog playlist. Empty until the playlist exists; the section
// hides itself rather than showing an empty shelf, because an empty
// changelog is worse evidence than no changelog.
const CHANGELOG_PLAYLIST_ID = ""

const GITHUB_URL = "https://github.com/afraaz-llc/archive336"

type SupportMessage = {
  id: string
  kind: string
  body: string
  fromStaff: boolean
  createdAt: string | null
}

/**
 * Support lives as a Settings tab rather than its own sidebar entry.
 *
 * Exported as a panel rather than a page: the tab renders it inside
 * Settings' chrome, and /support stays as a redirect so the links
 * already sent out in reply emails keep working.
 */
export function SupportPanel() {
  useDocumentTitle("Support")
  const { toast } = useToast()
  const [messages, setMessages] = React.useState<SupportMessage[]>([])

  // Newest message is at the bottom, so land there rather than at the
  // top of the history. Jumped, not smooth-scrolled: this UI does not
  // animate, and an animated scroll on load is motion nobody asked for.
  const scrollRef = React.useRef<HTMLDivElement>(null)

  React.useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages])
  const [body, setBody] = React.useState("")
  // Grow the composer to fit what has been typed, instead of a drag
  // handle. Height is reset before measuring so it shrinks again on
  // delete; min-h/max-h in the class list bound it, and past the
  // maximum the textarea scrolls rather than swallowing the history.
  const composerRef = React.useRef<HTMLTextAreaElement>(null)
  React.useEffect(() => {
    const el = composerRef.current
    if (!el) return
    el.style.height = "0px"
    el.style.height = `${el.scrollHeight}px`
  }, [body])
  const [sending, setSending] = React.useState(false)

  const load = React.useCallback(() => {
    fetch("/api/support/thread", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => d && setMessages(d.messages as SupportMessage[]))
      .catch(() => {})
  }, [])

  React.useEffect(() => {
    load()
    // A reply arrives while the tab sits open in another window. Same
    // reason the worker app re-reads on focus: coming back IS the
    // gesture of "did anything happen while I was away".
    window.addEventListener("focus", load)
    return () => window.removeEventListener("focus", load)
  }, [load])

  const send = async () => {
    const text = body.trim()
    if (!text || sending) return
    setSending(true)
    try {
      const res = await fetch("/api/support/messages", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ body: text }),
      }).catch(() => null)
      if (!res || !res.ok) {
        toast({
          title: "Couldn't send that",
          description: res ? `Server returned ${res.status}.` : "Backend unreachable.",
          variant: "error",
        })
        return
      }
      setBody("")
      load()
      toast({ title: "Sent - you'll get a reply by email" })
    } finally {
      setSending(false)
    }
  }

  return (
    <div className="max-w-4xl">
      <p className="text-sm text-muted-foreground max-w-[60ch] leading-relaxed">
        ARCHIVE336 is built and maintained by one person. If something is
        wrong, or you want it to do something it doesn't, say so - it gets
        read.
      </p>

      <section className="mt-10">
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">
          Support chat
        </div>

        {/* One box holding the whole conversation, composer included.
            Every message used to be its own bordered box stacked above a
            separate composer box, which read as a list of notices rather
            than a conversation - and grew the page without bound as the
            thread got longer. The history scrolls inside a fixed height
            so the reply field stays where the user left it. */}
        <div
          className={
            "border border-border " +
            // Fixed height once there is a conversation: the composer
            // grows UP into the history rather than pushing the box -
            // and everything below it - further down the page. Natural
            // height when empty, since a fixed well with nothing in it
            // reads as something that failed to load.
            (messages.length > 0 ? "flex flex-col h-[30rem]" : "")
          }
        >
          {messages.length > 0 ? (
            <div
              ref={scrollRef}
              className="flex-1 min-h-0 overflow-y-auto p-4 space-y-4"
            >
              {messages.map((m) => (
                <div
                  key={m.id}
                  className={
                    "pl-3 border-l-2 " +
                    (m.fromStaff ? "border-white" : "border-border")
                  }
                >
                  <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-1.5">
                    {m.fromStaff ? "Afraaz" : "You"}
                    {m.createdAt && (
                      <span className="ml-2 opacity-60 font-mono normal-case tracking-normal">
                        {formatRelativeDate(m.createdAt)}
                      </span>
                    )}
                  </div>
                  <p className="text-sm leading-relaxed whitespace-pre-wrap break-words">
                    {m.body}
                  </p>
                </div>
              ))}
            </div>
          ) : (
            /* No fixed height when there is nothing to scroll - an empty
               26rem well reads as something failing to load. */
            <div className="px-4 pt-4 text-sm text-muted-foreground">
              No messages yet.
            </div>
          )}

          <div className="border-t border-border p-4 shrink-0">
          <textarea
            ref={composerRef}
            value={body}
            onChange={(e) => setBody(e.target.value)}
            className="w-full border border-border bg-transparent p-3 text-sm text-foreground outline-none placeholder:text-muted-foreground focus:border-white focus:bg-white/5 resize-none overflow-y-auto min-h-28 max-h-60"
          />
          <div className="flex items-center justify-end gap-4 mt-3">
            <Button onClick={() => void send()} disabled={!body.trim() || sending}>
              {sending ? "Sending…" : "Send"}
            </Button>
          </div>
          </div>
        </div>
      </section>

      <section className="mt-10">
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">
          Resources
        </div>

        {/* Two equal cards rather than a video embed above a card.
            Side by side they are one row of links out; an inline
            iframe would tower over the thing beside it, and pull
            YouTube's player into a settings tab to do it.

            The changelog slot is SHOWN before the playlist id is set,
            rather than hidden. Hidden meant the only way to learn the
            feature existed was to read the source. */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          {CHANGELOG_PLAYLIST_ID ? (
            <a
              href={`https://www.youtube.com/playlist?list=${CHANGELOG_PLAYLIST_ID}`}
              target="_blank"
              rel="noreferrer"
              className="flex items-center gap-4 border border-border p-4"
            >
              <PlaySquare className="size-5 shrink-0" />
              <div className="min-w-0 flex-1 text-sm font-semibold">
                Changelog
              </div>
              <ExternalLink className="size-4 text-muted-foreground shrink-0" />
            </a>
          ) : (
            <div className="flex items-center gap-4 border border-dashed border-border p-4">
              <PlaySquare className="size-5 shrink-0 text-muted-foreground" />
              <div className="min-w-0 flex-1 text-sm font-semibold text-muted-foreground">
                Changelog
              </div>
              <span className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold shrink-0">
                Soon
              </span>
            </div>
          )}

          <a
            href={GITHUB_URL}
            target="_blank"
            rel="noreferrer"
            className="flex items-center gap-4 border border-border p-4"
          >
            <Code2 className="size-5 shrink-0" />
            <div className="min-w-0 flex-1 text-sm font-semibold truncate">
              afraaz-llc/archive336
            </div>
            <ExternalLink className="size-4 text-muted-foreground shrink-0" />
          </a>
        </div>
      </section>
    </div>
  )
}
