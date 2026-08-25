import * as React from "react"

export type User = {
  id: string
  username: string
  email: string
  email_verified: boolean
  is_admin: boolean
  created_at: string
  // Real DB tier. Default "basic" until we wire others.
  tier: string
  // Admin-only impersonation set from /dev. NULL on normal accounts
  // and on admin accounts that haven't flipped the toggle.
  tier_override: string | null
  // tier_override ?? tier. Branch all tier-aware UI on this.
  effective_tier: string
}

type AuthState =
  | { status: "loading" }
  | { status: "authed"; user: User }
  | { status: "anon" }

type AuthCtx = {
  state: AuthState
  refresh: () => Promise<void>
  setAuthed: (user: User) => void
  logout: () => Promise<void>
}

const Ctx = React.createContext<AuthCtx | null>(null)

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = React.useState<AuthState>({ status: "loading" })

  const refresh = React.useCallback(async () => {
    try {
      const res = await fetch("/api/auth/me", { credentials: "include" })
      if (res.ok) {
        const user = (await res.json()) as User
        setState({ status: "authed", user })
      } else {
        setState({ status: "anon" })
      }
    } catch {
      setState({ status: "anon" })
    }
  }, [])

  const setAuthed = React.useCallback((user: User) => {
    setState({ status: "authed", user })
  }, [])

  const logout = React.useCallback(async () => {
    try {
      await fetch("/api/auth/logout", {
        method: "POST",
        credentials: "include",
      })
    } finally {
      setState({ status: "anon" })
    }
  }, [])

  React.useEffect(() => {
    void refresh()
  }, [refresh])

  const value = React.useMemo(
    () => ({ state, refresh, setAuthed, logout }),
    [state, refresh, setAuthed, logout]
  )

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useAuth(): AuthCtx {
  const ctx = React.useContext(Ctx)
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>")
  return ctx
}
