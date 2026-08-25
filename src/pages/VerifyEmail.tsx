import * as React from "react"
import { useNavigate, useSearchParams } from "react-router-dom"
import { useAuth } from "@/auth/AuthContext"

/**
 * Public landing page for the email-verification link.
 *
 * Reads ?token=xxx from the URL and POSTs it to /api/auth/verify-email.
 * No login required — the token IS the proof of email control.
 *
 * Three terminal states: success, error, missing-token. Auto-redirects
 * after success aren't worth it here since the user might be on a
 * different device than the one with their session, so we just show
 * a "go to settings" link they can click manually.
 */
export default function VerifyEmail() {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const { refresh } = useAuth()
  const token = searchParams.get("token") || ""
  const [status, setStatus] = React.useState<
    "verifying" | "success" | "error" | "no-token"
  >(token ? "verifying" : "no-token")
  const [error, setError] = React.useState<string | null>(null)

  // Refresh /api/auth/me before navigating to /settings so the user's
  // freshly-flipped email_verified flag is in the auth context. Without
  // this, the email row in Settings briefly renders red until the user
  // hits refresh, since AuthContext caches the prior /me result.
  const goToSettings = React.useCallback(async () => {
    await refresh()
    navigate("/settings")
  }, [refresh, navigate])

  React.useEffect(() => {
    if (!token) return
    let cancelled = false
    ;(async () => {
      try {
        const res = await fetch("/api/auth/verify-email", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token }),
        })
        if (cancelled) return
        if (!res.ok && res.status !== 204) {
          const data = await res.json().catch(() => ({}))
          throw new Error(data?.detail || `HTTP ${res.status}`)
        }
        setStatus("success")
      } catch (e) {
        if (cancelled) return
        setError(e instanceof Error ? e.message : "Couldn't verify email.")
        setStatus("error")
      }
    })()
    return () => {
      cancelled = true
    }
  }, [token])

  return (
    <div className="min-h-screen bg-background text-foreground px-6 flex items-center justify-center">
      <div className="w-full max-w-md text-center space-y-6">
        <h1 className="text-4xl font-extrabold tracking-tight mb-12">
          ARCHIVE336
        </h1>

        {status === "verifying" && (
          <p className="text-sm text-muted-foreground">
            Verifying your email…
          </p>
        )}

        {status === "success" && (
          <>
            <h2 className="text-xl font-bold">Email verified</h2>
            <button
              type="button"
              onClick={() => void goToSettings()}
              className="inline-block text-xs uppercase tracking-wider text-muted-foreground font-semibold hover:text-foreground cursor-pointer"
            >
              Go to settings →
            </button>
          </>
        )}

        {status === "error" && (
          <>
            <h2 className="text-xl font-bold text-destructive">
              Couldn't verify email
            </h2>
            <p className="text-sm text-muted-foreground leading-relaxed">
              {error}
            </p>
            <button
              type="button"
              onClick={() => void goToSettings()}
              className="inline-block text-xs uppercase tracking-wider text-muted-foreground font-semibold hover:text-foreground cursor-pointer"
            >
              Go to settings →
            </button>
          </>
        )}

        {status === "no-token" && (
          <>
            <p className="text-sm text-destructive font-semibold">
              This verification link is missing its token.
            </p>
            <button
              type="button"
              onClick={() => void goToSettings()}
              className="inline-block text-xs uppercase tracking-wider text-muted-foreground font-semibold hover:text-foreground cursor-pointer"
            >
              Go to settings →
            </button>
          </>
        )}
      </div>
    </div>
  )
}
