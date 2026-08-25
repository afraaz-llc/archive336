import * as React from "react"
import { useNavigate } from "react-router-dom"
import { Plus, X } from "lucide-react"
import { useAuth } from "@/auth/AuthContext"
import { Button } from "@/components/ui/button"
import { useToast } from "@/components/ui/toast"

/**
 * Settings → Account → account switcher.
 *
 * Lists the accounts signed in on this browser (from GET /auth/accounts),
 * with the active one badged, a Switch button on the others, and a
 * per-account Sign out. "Add account" goes to the login page in add-mode
 * (?add=1), which logs into another account without dropping this one.
 *
 * Switching / removing-the-active-account do a full navigation rather
 * than a soft state update, so every page reloads its data as the newly
 * active account (no stale per-user state left over).
 */

type Account = {
  userId: string
  username: string
  email: string
  tier: string
  active: boolean
}

export function AccountSwitcher() {
  const { state } = useAuth()
  const navigate = useNavigate()
  const { toast } = useToast()
  const [accounts, setAccounts] = React.useState<Account[] | null>(null)
  const [busyId, setBusyId] = React.useState<string | null>(null)

  const activeUserId = state.status === "authed" ? state.user.id : null

  const load = React.useCallback(async () => {
    try {
      const res = await fetch("/api/auth/accounts", { credentials: "include" })
      setAccounts(res.ok ? ((await res.json()) as Account[]) : [])
    } catch {
      setAccounts([])
    }
  }, [])

  React.useEffect(() => {
    void load()
  }, [load])

  const switchTo = async (userId: string) => {
    if (busyId) return
    setBusyId(userId)
    try {
      const res = await fetch("/api/auth/accounts/switch", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ userId }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      // Hard navigation so the whole app re-initializes as the new account.
      window.location.assign("/settings?tab=account")
    } catch {
      setBusyId(null)
      toast({ title: "Couldn't switch accounts", variant: "error" })
    }
  }

  const signOut = async (userId: string) => {
    if (busyId) return
    setBusyId(userId)
    try {
      const res = await fetch("/api/auth/accounts/remove", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ userId }),
      })
      const data = res.ok
        ? ((await res.json()) as { activeUserId: string | null })
        : null
      if (userId === activeUserId) {
        // We signed out the active account — the backend promoted another
        // or cleared everything. Reload to pick up whoever's active now.
        window.location.assign(data?.activeUserId ? "/settings?tab=account" : "/auth")
      } else {
        await load() // a non-active account left; just refresh the list
        setBusyId(null)
      }
    } catch {
      setBusyId(null)
      toast({ title: "Couldn't sign out account", variant: "error" })
    }
  }

  return (
    <div className="border border-border divide-y divide-border">
      {accounts === null ? (
        <div className="p-4 text-sm text-muted-foreground">Loading accounts…</div>
      ) : (
        accounts.map((a) => (
          <div
            key={a.userId}
            className="flex items-center justify-between gap-4 p-4"
          >
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-sm font-semibold">{a.username}</span>
                {a.active && (
                  <span className="text-[10px] uppercase tracking-wider font-bold border border-foreground/40 px-1.5 py-0.5">
                    Active
                  </span>
                )}
              </div>
              <div className="text-xs text-muted-foreground font-mono truncate">
                {a.email}
              </div>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              {!a.active && (
                <Button
                  variant="outline"
                  size="sm"
                  disabled={busyId === a.userId}
                  onClick={() => switchTo(a.userId)}
                >
                  {busyId === a.userId ? "Switching…" : "Switch"}
                </Button>
              )}
              {!a.active && (
                <button
                  type="button"
                  onClick={() => signOut(a.userId)}
                  disabled={busyId === a.userId}
                  aria-label={`Sign out ${a.username}`}
                  title="Sign out"
                  className="size-8 flex items-center justify-center border border-border text-muted-foreground hover:text-destructive hover:border-destructive cursor-pointer disabled:opacity-50 disabled:cursor-default"
                >
                  <X className="size-4" />
                </button>
              )}
            </div>
          </div>
        ))
      )}
      <button
        type="button"
        onClick={() => navigate("/auth?add=1")}
        className="w-full flex items-center justify-center gap-2 p-3 text-sm font-semibold text-muted-foreground hover:text-foreground cursor-pointer"
      >
        <Plus className="size-4" /> Add account
      </button>
    </div>
  )
}
