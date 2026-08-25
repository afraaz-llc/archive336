import * as React from "react"
import { Link, useNavigate, useSearchParams } from "react-router-dom"

/**
 * Public landing page for the deletion confirmation link in the
 * verification email. Reads ?token=xxx and POSTs to
 * /api/auth/me/confirm-delete - the server validates the token and
 * runs the actual wipe. On success we redirect to the static
 * /account-deleted page; on failure we show the error.
 *
 * No login required - the token IS the proof of intent + email
 * control. This lets users confirm from any device.
 */
export default function ConfirmDelete() {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const token = searchParams.get("token") || ""
  const [status, setStatus] = React.useState<
    "confirming" | "error" | "no-token"
  >(token ? "confirming" : "no-token")
  const [error, setError] = React.useState<string | null>(null)

  React.useEffect(() => {
    if (!token) return
    let cancelled = false
    ;(async () => {
      try {
        const res = await fetch("/api/auth/me/confirm-delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token }),
        })
        if (cancelled) return
        if (res.ok || res.status === 204) {
          navigate("/account-deleted", { replace: true })
          return
        }
        const data = await res.json().catch(() => ({}))
        throw new Error(data?.detail || `HTTP ${res.status}`)
      } catch (e) {
        if (cancelled) return
        setError(
          e instanceof Error ? e.message : "Couldn't delete the account."
        )
        setStatus("error")
      }
    })()
    return () => {
      cancelled = true
    }
  }, [token, navigate])

  return (
    <div className="min-h-screen bg-background text-foreground px-6 flex items-center justify-center">
      <div className="w-full max-w-md text-center space-y-6">
        <h1 className="text-4xl font-extrabold tracking-tight mb-12">
          ARCHIVE336
        </h1>

        {status === "confirming" && (
          <p className="text-sm text-muted-foreground">
            Deleting your account…
          </p>
        )}

        {status === "error" && (
          <>
            <h2 className="text-xl font-bold text-destructive">
              Couldn't delete account
            </h2>
            <p className="text-sm text-muted-foreground leading-relaxed">
              {error}
            </p>
            <Link
              to="/settings"
              className="inline-block text-xs uppercase tracking-wider text-muted-foreground font-semibold hover:text-foreground"
            >
              Back to settings →
            </Link>
          </>
        )}

        {status === "no-token" && (
          <p className="text-sm text-destructive font-semibold">
            This deletion link is missing its token.
          </p>
        )}
      </div>
    </div>
  )
}
