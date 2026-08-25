import * as React from "react"
import { Link, useNavigate, useSearchParams } from "react-router-dom"
import { cn } from "@/lib/utils"
import { useAuth, type User } from "@/auth/AuthContext"

type Mode = "signup" | "login"

function readModeFromHash(): Mode {
  if (typeof window === "undefined") return "login"
  const h = window.location.hash.replace(/^#/, "")
  return h === "signup" ? "signup" : "login"
}

export default function Auth() {
  const navigate = useNavigate()
  const { state, setAuthed } = useAuth()
  const [searchParams] = useSearchParams()
  // "Add another account" mode (from the Settings account switcher):
  // logging in keeps the current account signed in instead of replacing
  // it, and we land back on the switcher afterward.
  const addMode = searchParams.get("add") === "1"
  const [mode, setMode] = React.useState<Mode>(readModeFromHash)
  const [username, setUsername] = React.useState("")
  const [email, setEmail] = React.useState("")
  const [password, setPassword] = React.useState("")
  const [error, setError] = React.useState<string | null>(null)
  const [busy, setBusy] = React.useState(false)

  // Center the form vertically on first paint, then lock that position.
  // Toggling Sign up / Log in won't re-center the structure after this runs.
  const contentRef = React.useRef<HTMLDivElement>(null)
  const [topOffset, setTopOffset] = React.useState<number | null>(null)

  React.useLayoutEffect(() => {
    if (!contentRef.current) return
    const formHeight = contentRef.current.offsetHeight
    const viewportHeight = window.innerHeight
    setTopOffset(Math.max(32, Math.round((viewportHeight - formHeight) / 2)))
  }, [])

  // Keep mode in sync if the hash changes externally (back/forward, manual edit).
  React.useEffect(() => {
    const onHashChange = () => setMode(readModeFromHash())
    window.addEventListener("hashchange", onHashChange)
    return () => window.removeEventListener("hashchange", onHashChange)
  }, [])

  // If the user is already authed, bounce them into the app — unless
  // we're adding another account, where being authed is expected.
  React.useEffect(() => {
    if (!addMode && state.status === "authed") {
      navigate("/", { replace: true })
    }
  }, [state.status, navigate, addMode])

  const switchMode = (next: Mode) => {
    if (next === mode) return
    setMode(next)
    setPassword("")
    setError(null)
    const nextHash = next === "signup" ? "#signup" : "#login"
    // Preserve the query string (notably ?add=1) so switching tabs while
    // adding an account doesn't silently drop out of add-account mode.
    window.history.replaceState(
      null,
      "",
      window.location.pathname + window.location.search + nextHash
    )
  }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)

    const u = username.trim()
    if (!u)
      return setError(
        mode === "signup"
          ? "Enter a username."
          : "Enter your username or email."
      )
    if (!password) return setError("Enter a password.")
    if (mode === "signup") {
      if (!email.trim()) return setError("Enter an email.")
    }

    setBusy(true)
    try {
      // In add-account mode, BOTH signup and login carry ?add=true so the
      // backend keeps the current account signed in (switcher bundle).
      const base = mode === "signup" ? "/api/auth/signup" : "/api/auth/login"
      const url = addMode ? `${base}?add=true` : base
      const body =
        mode === "signup"
          ? { username: u, email: email.trim(), password }
          : { username: u, password }
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        let detail = "Something went wrong."
        try {
          const data = await res.json()
          if (typeof data?.detail === "string") detail = data.detail
          else if (Array.isArray(data?.detail) && data.detail.length > 0) {
            detail = data.detail[0]?.msg ?? detail
          }
        } catch {
          /* ignore */
        }
        setError(detail)
        setBusy(false)
        return
      }
      const user = (await res.json()) as User
      // The signup endpoint auto-fires a verification email. Seed the
      // local cooldown timestamp now so the Verify-email button in
      // Settings shows the correct "Sent · 1h" state on first load
      // instead of inviting a click that would 429.
      if (mode === "signup") {
        try {
          localStorage.setItem(
            `aether_verify_sent_at_${user.id}`,
            String(Date.now())
          )
        } catch {
          /* localStorage disabled — fine, the 429 fallback still kicks in */
        }
      }
      setAuthed(user)
      // Brand-new accounts go straight to /settings#payment so they
      // see the card-setup flow before anything else - landing on Home
      // would just dead-end into a 402 the moment they try to do
      // anything. Existing logins continue to land on Home.
      navigate(
        addMode
          ? "/settings?tab=account"
          : mode === "signup"
          ? "/settings#payment"
          : "/",
        { replace: true }
      )
    } catch {
      setError("Couldn't reach the server. Is the backend running?")
      setBusy(false)
    }
  }

  return (
    <div className="min-h-screen bg-background text-foreground px-6 pb-12">
      {/* Escape hatch, styled + positioned to match the ARCHIVE336 wordmark
          in the landing header (same top-left gutter, font, size, weight). In
          "add account" mode it returns to the account switcher it was launched
          from; otherwise it goes back to the landing/home. */}
      <Link
        to={addMode ? "/settings?tab=account" : "/"}
        className="fixed top-0 left-0 z-20 inline-flex items-center gap-1.5 px-6 py-3.5 text-[15px] font-extrabold tracking-[0.02em] text-foreground"
      >
        <span aria-hidden>←</span> Back
      </Link>
      <div
        ref={contentRef}
        className="w-full max-w-md mx-auto"
        style={{
          marginTop: topOffset != null ? `${topOffset}px` : undefined,
          visibility: topOffset != null ? "visible" : "hidden",
        }}
      >
        <div className="mb-14 text-center select-none">
          <Link to="/" className="inline-block">
            <h1 className="text-4xl font-extrabold tracking-tight">
              ARCHIVE336
            </h1>
          </Link>
        </div>

        <div className="flex mb-4">
          <TabButton active={mode === "signup"} onClick={() => switchMode("signup")}>
            Sign up
          </TabButton>
          <TabButton active={mode === "login"} onClick={() => switchMode("login")}>
            Log in
          </TabButton>
        </div>

        <form onSubmit={submit} className="space-y-3" noValidate>
          <Field
            label={mode === "signup" ? "Username" : "Username or email"}
            value={username}
            onChange={setUsername}
            autoComplete="username"
            autoFocus
            disabled={busy}
          />
          {mode === "signup" && (
            <Field
              label="Email"
              type="email"
              value={email}
              onChange={setEmail}
              autoComplete="email"
              disabled={busy}
            />
          )}
          <Field
            label="Password"
            type="password"
            value={password}
            onChange={setPassword}
            autoComplete={mode === "signup" ? "new-password" : "current-password"}
            disabled={busy}
          />

          <button
            type="submit"
            disabled={busy}
            className="block w-full h-16 bg-white text-black text-2xl font-extrabold tracking-[0.2em] uppercase active:bg-neutral-300 disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer border-2 border-white mt-4"
          >
            {busy ? "…" : "Enter"}
          </button>

          <div
            className="h-5 text-sm text-destructive font-semibold text-center mt-3"
            role="alert"
          >
            {error}
          </div>

          {mode === "login" && (
            <div className="text-center">
              <Link
                to="/forgot-password"
                className="text-xs uppercase tracking-wider text-muted-foreground font-semibold hover:text-foreground"
              >
                Forgot password?
              </Link>
            </div>
          )}
        </form>
      </div>
    </div>
  )
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "flex-1 h-12 text-base font-bold cursor-pointer border-2 border-white",
        active
          ? "bg-neutral-500 text-black"
          : "bg-white text-black"
      )}
    >
      {children}
    </button>
  )
}

function Field({
  label,
  value,
  onChange,
  type = "text",
  autoComplete,
  autoFocus,
  disabled,
}: {
  label: string
  value: string
  onChange: (v: string) => void
  type?: string
  autoComplete?: string
  autoFocus?: boolean
  disabled?: boolean
}) {
  return (
    <input
      type={type}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={label}
      autoComplete={autoComplete}
      autoFocus={autoFocus}
      disabled={disabled}
      spellCheck={false}
      className="block w-full h-14 bg-transparent border-2 border-white px-4 text-white text-lg font-semibold placeholder:text-white placeholder:font-semibold outline-none focus:bg-white/5 disabled:opacity-60"
    />
  )
}
