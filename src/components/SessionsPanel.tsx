import * as React from "react"
import { LogOut } from "lucide-react"
import { Button } from "./ui/button"
import { useToast } from "./ui/toast"
import { formatRelativeDate } from "@/lib/format"

/**
 * Session management panel for Settings. Lists every active session
 * for the current user with one-click revoke per row plus a
 * "Sign out everywhere else" action that drops every session except
 * the one whose cookie is reading the page.
 *
 * The session whose cookie made the request comes back from the API
 * with `current: true`; its revoke button is hidden because revoking
 * your own session is what the Logout button is for.
 */

type ApiSession = {
  token: string
  createdAt: string | null
  expiresAt: string | null
  userAgent: string | null
  ipAddress: string | null
  current: boolean
}

export function SessionsPanel() {
  const { toast } = useToast()
  const [sessions, setSessions] = React.useState<ApiSession[] | null>(null)
  const [revokingToken, setRevokingToken] = React.useState<string | null>(null)
  const [revokingAll, setRevokingAll] = React.useState(false)

  const refresh = React.useCallback(async () => {
    try {
      const res = await fetch("/api/auth/sessions", { credentials: "include" })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = (await res.json()) as ApiSession[]
      setSessions(data)
    } catch {
      toast({
        title: "Couldn't load sessions",
        description: "Backend unreachable.",
        variant: "error",
      })
    }
  }, [toast])

  React.useEffect(() => {
    void refresh()
  }, [refresh])

  const revoke = async (token: string) => {
    setRevokingToken(token)
    try {
      const res = await fetch(
        `/api/auth/sessions/${encodeURIComponent(token)}`,
        { method: "DELETE", credentials: "include" }
      )
      if (!res.ok && res.status !== 204) {
        throw new Error(`HTTP ${res.status}`)
      }
      setSessions((curr) => (curr ?? []).filter((s) => s.token !== token))
    } catch {
      toast({
        title: "Couldn't revoke session",
        description: "Try again in a moment.",
        variant: "error",
      })
    } finally {
      setRevokingToken(null)
    }
  }

  const revokeOthers = async () => {
    setRevokingAll(true)
    try {
      const res = await fetch("/api/auth/sessions", {
        method: "DELETE",
        credentials: "include",
      })
      if (!res.ok && res.status !== 204) {
        throw new Error(`HTTP ${res.status}`)
      }
      setSessions((curr) => (curr ?? []).filter((s) => s.current))
      toast({
        title: "Signed out of other sessions",
        variant: "success",
      })
    } catch {
      toast({
        title: "Couldn't sign out other sessions",
        description: "Try again in a moment.",
        variant: "error",
      })
    } finally {
      setRevokingAll(false)
    }
  }

  if (sessions === null) {
    return (
      <div className="text-sm text-muted-foreground">Loading sessions...</div>
    )
  }

  const otherCount = sessions.filter((s) => !s.current).length

  return (
    <div className="border border-border divide-y divide-border">
      {sessions.map((s) => (
        <SessionRow
          key={s.token}
          session={s}
          busy={revokingToken === s.token}
          onRevoke={() => revoke(s.token)}
          otherCount={s.current ? otherCount : 0}
          onRevokeOthers={revokeOthers}
          revokingAll={revokingAll}
        />
      ))}
    </div>
  )
}

function SessionRow({
  session,
  busy,
  onRevoke,
  otherCount,
  onRevokeOthers,
  revokingAll,
}: {
  session: ApiSession
  busy: boolean
  onRevoke: () => void
  // Only set on the current-session row. When > 0 the row renders a
  // 'Sign out other sessions (N)' button in place of the per-row
  // Revoke button (which doesn't exist on the current row anyway).
  otherCount: number
  onRevokeOthers: () => void
  revokingAll: boolean
}) {
  const { label, kind } = parseUserAgent(session.userAgent)
  return (
    <div className="flex items-center justify-between gap-4 p-4">
      <div className="min-w-0 flex-1 space-y-1">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-sm font-semibold">{label}</span>
          {kind === "worker" && (
            <span className="text-[10px] uppercase tracking-wider font-bold border border-foreground/40 px-1.5 py-0.5">
              Worker app
            </span>
          )}
          {session.current && (
            <span className="text-[10px] uppercase tracking-wider font-bold border border-foreground/40 px-1.5 py-0.5">
              This device
            </span>
          )}
        </div>
        <div className="text-xs text-muted-foreground font-mono tabular-nums">
          {session.ipAddress ?? "Unknown IP"}
          {session.createdAt && (
            <>
              {" · "}signed in {formatRelativeDate(session.createdAt)}
            </>
          )}
        </div>
      </div>
      {!session.current && (
        <button
          type="button"
          onClick={onRevoke}
          disabled={busy}
          className="text-xs font-bold uppercase tracking-wider border border-destructive text-destructive hover:bg-destructive hover:text-destructive-foreground px-3 h-7 cursor-pointer disabled:opacity-50 disabled:cursor-default"
        >
          {busy ? "..." : "Revoke"}
        </button>
      )}
      {session.current && otherCount > 0 && (
        <Button
          variant="outline"
          size="sm"
          onClick={onRevokeOthers}
          disabled={revokingAll}
        >
          <LogOut />
          {revokingAll
            ? "Signing out..."
            : `Sign out other ${
                otherCount === 1 ? "session" : `sessions (${otherCount})`
              }`}
        </Button>
      )}
    </div>
  )
}

type SessionKind = "browser" | "worker" | "unknown"
type ParsedUA = { label: string; kind: SessionKind }

/**
 * Cheap user-agent → display info parser. Returns a label for the
 * primary line and a kind so the row can render a type pill. The real
 * UA spec is a disaster but we only care about turning the most
 * common UA strings into something humanish.
 */
function parseUserAgent(ua: string | null): ParsedUA {
  if (!ua) return { label: "Unknown device", kind: "unknown" }

  // First-party desktop worker. UA format is
  //   ARCHIVE336-Archive-Tool-Desktop/<version> (<hostname>)
  // We pull the hostname out of the parens for the label (with the
  // .local mDNS suffix stripped if present), and tag the row as a
  // worker so it gets the WORKER pill.
  const workerMatch = ua.match(
    /^ARCHIVE336-Archive-Tool-Desktop\/\S+\s*(?:\(([^)]+)\))?/
  )
  if (workerMatch) {
    const hostname = (workerMatch[1] ?? "").replace(/\.local$/, "").trim()
    return {
      label: hostname || "ARCHIVE336 Worker",
      kind: "worker",
    }
  }

  let os = "Unknown OS"
  if (/Windows NT/.test(ua)) os = "Windows"
  else if (/Mac OS X|Macintosh/.test(ua)) os = "macOS"
  else if (/Android/.test(ua)) os = "Android"
  else if (/iPhone|iPad|iOS/.test(ua)) os = "iOS"
  else if (/Linux/.test(ua)) os = "Linux"

  let browser = "Unknown browser"
  // Order matters - many browsers fork Chromium and inject their own
  // token after the Chrome one, so check the more specific ones first.
  if (/Edg\//.test(ua)) browser = "Edge"
  else if (/OPR\//.test(ua)) browser = "Opera"
  else if (/Firefox\//.test(ua)) browser = "Firefox"
  else if (/Chrome\//.test(ua)) browser = "Chrome"
  else if (/Safari\//.test(ua)) browser = "Safari"

  return { label: `${browser} on ${os}`, kind: "browser" }
}
