import * as React from "react"
import { Link, useNavigate, useSearchParams } from "react-router-dom"

/**
 * Reset password page — public, redeems the token from the email link.
 *
 * The token comes in via ?token=xxx in the URL. After a successful
 * reset, all of the user's existing sessions are invalidated by the
 * backend, so we send them to /auth to log in with the new password.
 */
export default function ResetPassword() {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const token = searchParams.get("token") || ""

  const [password, setPassword] = React.useState("")
  const [confirm, setConfirm] = React.useState("")
  const [busy, setBusy] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)
  const [done, setDone] = React.useState(false)

  // Token missing entirely — render an error state with no form to fill
  // (avoids letting users waste time typing a new password against a
  // link that can't possibly work).
  if (!token) {
    return (
      <div className="min-h-screen bg-background text-foreground px-6 py-16">
        <div className="w-full max-w-md mx-auto text-center space-y-6">
          <h1 className="text-4xl font-extrabold tracking-tight">
            ARCHIVE336
          </h1>
          <p className="text-sm text-destructive font-semibold">
            This reset link is missing its token. Request a new one.
          </p>
          <Link
            to="/forgot-password"
            className="inline-block text-xs uppercase tracking-wider text-muted-foreground font-semibold hover:text-foreground"
          >
            ← Request a new link
          </Link>
        </div>
      </div>
    )
  }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (busy) return
    if (!password) {
      setError("Enter a new password.")
      return
    }
    if (password !== confirm) {
      setError("Passwords don't match.")
      return
    }
    setBusy(true)
    setError(null)
    try {
      const res = await fetch("/api/auth/reset-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, new_password: password }),
      })
      if (!res.ok && res.status !== 204) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data?.detail || `HTTP ${res.status}`)
      }
      setDone(true)
      // Brief delay so the user sees the confirmation state before we
      // bounce them to the login page.
      setTimeout(() => navigate("/auth", { replace: true }), 1800)
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't reach the server.")
      setBusy(false)
    }
  }

  return (
    <div className="min-h-screen bg-background text-foreground px-6 py-16">
      <div className="w-full max-w-md mx-auto">
        <div className="mb-12 text-center select-none">
          <h1 className="text-4xl font-extrabold tracking-tight">
            ARCHIVE336
          </h1>
        </div>

        {done ? (
          <div className="text-center space-y-6">
            <h2 className="text-xl font-bold">Password updated</h2>
            <p className="text-sm text-muted-foreground leading-relaxed">
              Your password has been reset. Redirecting you to the login
              page…
            </p>
          </div>
        ) : (
          <>
            <h2 className="text-lg font-bold mb-2 text-center">
              Choose a new password
            </h2>
            <p className="text-sm text-muted-foreground mb-8 text-center leading-relaxed">
              Resetting your password will sign you out of every device.
            </p>

            <form onSubmit={submit} className="space-y-3" noValidate>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="New password"
                autoComplete="new-password"
                autoFocus
                required
                disabled={busy}
                className="block w-full h-14 bg-transparent border-2 border-white px-4 text-white text-lg font-semibold placeholder:text-white placeholder:font-semibold outline-none focus:bg-white/5 disabled:opacity-60"
              />
              <input
                type="password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                placeholder="Confirm password"
                autoComplete="new-password"
                required
                disabled={busy}
                className="block w-full h-14 bg-transparent border-2 border-white px-4 text-white text-lg font-semibold placeholder:text-white placeholder:font-semibold outline-none focus:bg-white/5 disabled:opacity-60"
              />

              <button
                type="submit"
                disabled={busy || !password || !confirm}
                className="block w-full h-16 bg-white text-black text-2xl font-extrabold tracking-[0.2em] uppercase active:bg-neutral-300 disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer border-2 border-white mt-4"
              >
                {busy ? "…" : "Reset"}
              </button>

              <div
                className="h-5 text-sm text-destructive font-semibold text-center mt-3"
                role="alert"
              >
                {error}
              </div>
            </form>
          </>
        )}
      </div>
    </div>
  )
}
