import * as React from "react"
import { LogOut } from "lucide-react"
import { Button } from "./ui/button"
import { useAuth } from "@/auth/AuthContext"
import { formatFullDate, formatTimeUntil } from "@/lib/format"

/**
 * Account tab footer. A read-only row summarizing the current session's
 * remaining lifetime and the date it auto-expires, with a Log out
 * button on the right (the row itself is not clickable).
 *
 * Sessions last 30 days from sign-in and don't slide on activity, so
 * the countdown reflects a fixed expiry rather than an idle timeout.
 * The expiry comes from the `current: true` row of /api/auth/sessions.
 */
export function CurrentSessionCard() {
  const { logout } = useAuth()
  const [expiresAt, setExpiresAt] = React.useState<string | null>(null)
  const [loaded, setLoaded] = React.useState(false)

  React.useEffect(() => {
    let cancelled = false
    fetch("/api/auth/sessions", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((data: { current?: boolean; expiresAt?: string | null }[] | null) => {
        if (cancelled) return
        if (Array.isArray(data)) {
          const current = data.find((s) => s.current)
          setExpiresAt(current?.expiresAt ?? null)
        }
        setLoaded(true)
      })
      .catch(() => {
        if (!cancelled) setLoaded(true)
      })
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <div className="border border-border p-4 flex items-center justify-between gap-4">
      <div className="min-w-0 flex-1">
        {expiresAt ? (
          <div className="text-sm font-semibold">
            This session expires in {formatTimeUntil(expiresAt)}
            <span className="text-muted-foreground font-normal">
              {" · "}
              {formatFullDate(expiresAt)}
            </span>
          </div>
        ) : (
          <div className="text-sm font-semibold">
            Current session
            <span className="text-muted-foreground font-normal">
              {" · "}
              {loaded ? "Active on this device" : "Checking session…"}
            </span>
          </div>
        )}
      </div>
      <Button
        variant="outline"
        onClick={() => void logout()}
        className="shrink-0 border-destructive text-destructive"
      >
        <LogOut className="-scale-x-100" />
        Log out
      </Button>
    </div>
  )
}
