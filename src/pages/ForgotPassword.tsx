import * as React from "react"
import { Link } from "react-router-dom"

/**
 * Forgot password page — public, request a reset link by email.
 *
 * Always shows the same "If we found a matching account…" message
 * regardless of whether the email is on file. The backend mirrors that
 * (always returns 204) so attackers can't probe registered addresses.
 */
export default function ForgotPassword() {
  const [email, setEmail] = React.useState("")
  const [busy, setBusy] = React.useState(false)
  const [submitted, setSubmitted] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      const res = await fetch("/api/auth/forgot-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      })
      if (!res.ok && res.status !== 204) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data?.detail || `HTTP ${res.status}`)
      }
      setSubmitted(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't reach the server.")
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="min-h-screen bg-background text-foreground px-6 py-16 flex flex-col items-center justify-center">
      <div className="w-full max-w-md">
        <div className="mb-12 text-center select-none">
          <h1 className="text-4xl font-extrabold tracking-tight">
            ARCHIVE336
          </h1>
        </div>

        {submitted ? (
          <div className="text-center space-y-6">
            <h2 className="text-xl font-bold">Check your inbox</h2>
            <p className="text-sm text-muted-foreground leading-relaxed">
              If an account exists for{" "}
              <span className="font-mono">{email}</span> a link has been
              sent to reset your password.
            </p>
            <Link
              to="/auth"
              className="inline-block text-xs uppercase tracking-wider text-muted-foreground font-semibold hover:text-foreground"
            >
              ← Back to login
            </Link>
          </div>
        ) : (
          <>
            <h2 className="text-lg font-bold mb-8 text-center">
              Reset your password
            </h2>

            <form onSubmit={submit} className="space-y-3" noValidate>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="Email"
                autoComplete="email"
                autoFocus
                required
                disabled={busy}
                spellCheck={false}
                className="block w-full h-14 bg-transparent border-2 border-white px-4 text-white text-lg font-semibold placeholder:text-white placeholder:font-semibold outline-none focus:bg-white/5 disabled:opacity-60"
              />

              <button
                type="submit"
                disabled={busy || !email}
                className="block w-full h-16 bg-white text-black text-2xl font-extrabold tracking-[0.2em] uppercase active:bg-neutral-300 disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer border-2 border-white mt-4"
              >
                {busy ? "…" : "Send link"}
              </button>

              <div
                className="h-5 text-sm text-destructive font-semibold text-center mt-3"
                role="alert"
              >
                {error}
              </div>
            </form>

            <div className="text-center mt-8">
              <Link
                to="/auth"
                className="text-xs uppercase tracking-wider text-muted-foreground font-semibold hover:text-foreground"
              >
                ← Back to login
              </Link>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
