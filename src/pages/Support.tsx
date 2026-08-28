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

// nocookie so watching the changelog does not hand YouTube a tracking
// cookie on our own site. Consistent with selling custody of private
// video to people who care where their data goes.
const EMBED_HOST = "https://www.youtube-nocookie.com"

const GITHUB_URL = "https://github.com/afraaz-llc/archive336"

type SupportMessage = {
  id: string
  kind: string
  body: string
  fromStaff: boolean
  createdAt: string | null
}

const KINDS: { value: string; label: string }[] = [
  { value: "bug", label: "Something's broken" },
  { value: "feature", label: "I want a feature" },
  { value: "question", label: "A question" },
]

export default function Support() {
  useDocumentTitle("Support")
  const { toast } = useToast()
  const [messages, setMessages] = React.useState<SupportMessage[]>([])
  const [kind, setKind] = React.useState("question")
  const [body, setBody] = React.useState("")
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
        body: JSON.stringify({ body: text, kind }),
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
    <div className="p-8 max-w-4xl">
      <h1 className="text-2xl font-extrabold tracking-tight">Support</h1>
      <p className="mt-2 text-sm text-muted-foreground max-w-[60ch] leading-relaxed">
        ARCHIVE336 is built and maintained by one person. If something is
        wrong, or you want it to do something it doesn't, say so - it gets
        read.
      </p>

      {CHANGELOG_PLAYLIST_ID && (
        <section className="mt-10">
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">
            Changelog
          </div>
          <div className="border border-border">
            <iframe
              className="w-full aspect-video block"
              src={`${EMBED_HOST}/embed/videoseries?list=${CHANGELOG_PLAYLIST_ID}`}
              title="ARCHIVE336 changelog"
              allow="accelerometer; clipboard-write; encrypted-media; picture-in-picture"
              allowFullScreen
            />
          </div>
          <a
            href={`https://www.youtube.com/playlist?list=${CHANGELOG_PLAYLIST_ID}`}
            target="_blank"
            rel="noreferrer"
            className="mt-3 inline-flex items-center gap-2 text-xs text-muted-foreground font-semibold"
          >
            <PlaySquare className="size-4" />
            Watch on YouTube
            <ExternalLink className="size-3" />
          </a>
        </section>
      )}

      <section className="mt-10">
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">
          Send a message
        </div>

        {messages.length > 0 && (
          <div className="space-y-2 mb-4">
            {messages.map((m) => (
              <div
                key={m.id}
                className={
                  m.fromStaff
                    ? "border-2 border-white p-4"
                    : "border border-border p-4"
                }
              >
                <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-2">
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
        )}

        <div className="border border-border p-4">
          <div className="flex flex-wrap gap-1.5 mb-3">
            {KINDS.map((k) => (
              <button
                key={k.value}
                type="button"
                onClick={() => setKind(k.value)}
                className={
                  "px-3 py-1 text-xs font-semibold border cursor-pointer " +
                  (kind === k.value
                    ? "bg-white text-black border-white"
                    : "border-border text-foreground")
                }
              >
                {k.label}
              </button>
            ))}
          </div>
          <textarea
            value={body}
            onChange={(e) => setBody(e.target.value)}
            rows={5}
            placeholder="What happened?"
            className="w-full border border-border bg-transparent p-3 text-sm text-foreground outline-none placeholder:text-muted-foreground focus:border-white focus:bg-white/5 resize-y"
          />
          <div className="flex items-center justify-between gap-4 mt-3">
            {/* Said plainly rather than buried in a privacy note: the
                snapshot is the reason a reply can be useful, and hiding
                that it is attached would be the wrong kind of quiet. */}
            <p className="text-xs text-muted-foreground">
              Your account status is attached so I can see what's happening.
              Usually answered same day.
            </p>
            <Button onClick={() => void send()} disabled={!body.trim() || sending}>
              {sending ? "Sending…" : "Send"}
            </Button>
          </div>
        </div>
      </section>

      <section className="mt-10">
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">
          The code
        </div>
        <a
          href={GITHUB_URL}
          target="_blank"
          rel="noreferrer"
          className="flex items-center gap-4 border border-border p-4 hover:border-muted-foreground"
        >
          <Code2 className="size-5 shrink-0" />
          <div className="min-w-0 flex-1">
            <div className="text-sm font-semibold">afraaz-llc/archive336</div>
            <div className="text-xs text-muted-foreground">
              Every change to this product, in the open.
            </div>
          </div>
          <ExternalLink className="size-4 text-muted-foreground shrink-0" />
        </a>
      </section>
    </div>
  )
}
