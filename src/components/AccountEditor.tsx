import * as React from "react"
import { useAuth } from "@/auth/AuthContext"
import { Button } from "@/components/ui/button"
import { useToast } from "@/components/ui/toast"

/**
 * Settings → Account section.
 *
 * Two modes:
 *   - View: read-only labels + values, single [Edit] button bottom-right
 *   - Edit: every field becomes editable. New password is optional
 *     (blank = keep current). Current password is required to commit
 *     any change. One Save sends the whole diff to /api/auth/me.
 *
 * Atomic on the backend — partial failures (e.g. dupe username) leave
 * everything unchanged.
 */
export function AccountEditor() {
  const { state, refresh } = useAuth()
  const user = state.status === "authed" ? state.user : null
  const [editing, setEditing] = React.useState(false)
  const [busy, setBusy] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)

  // Form state — initialized from the current user when entering
  // edit mode. New password and current password start empty.
  const [username, setUsername] = React.useState("")
  const [email, setEmail] = React.useState("")
  const [newPassword, setNewPassword] = React.useState("")
  const [currentPassword, setCurrentPassword] = React.useState("")

  const startEdit = () => {
    if (!user) return
    setUsername(user.username)
    setEmail(user.email)
    setNewPassword("")
    setCurrentPassword("")
    setError(null)
    setEditing(true)
  }

  const cancel = () => {
    setEditing(false)
    setError(null)
  }

  const save = async (e: React.FormEvent) => {
    e.preventDefault()
    if (busy || !user) return
    setBusy(true)
    setError(null)
    try {
      const body: Record<string, string> = {
        current_password: currentPassword,
      }
      if (username !== user.username) body.new_username = username
      if (email !== user.email) body.new_email = email
      if (newPassword) body.new_password = newPassword

      const res = await fetch("/api/auth/me", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data?.detail || `HTTP ${res.status}`)
      }
      await refresh()
      setEditing(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't save changes.")
    } finally {
      setBusy(false)
    }
  }

  if (!user) {
    return (
      <div className="border border-border p-4 text-sm text-muted-foreground">
        Loading…
      </div>
    )
  }

  if (!editing) {
    // Card border turns red when the email isn't verified yet — the
    // "Verify" button on the email row is the call-to-action.
    //
    // Edit button position depends on verification state to avoid
    // overlapping with the Verify button on the email row:
    //   - verified  → top-right corner (clean, no other button there)
    //   - unverified→ bottom-right (Verify owns the right side of the
    //                  email row, so we move Edit down to stay clear)
    return (
      <div
        className={
          "relative border pt-4 pb-3 px-4 space-y-3 text-sm " +
          (user.email_verified ? "border-border" : "border-destructive")
        }
      >
        <EmailRow user={user} />
        <Row label="Username" value={user.username} />
        <Row label="Password" value="••••••••" />
        <div className="absolute right-3 bottom-3">
          <Button onClick={startEdit}>Edit</Button>
        </div>
      </div>
    )
  }

  // Save is enabled when current password is provided AND something
  // about the form differs from the saved values. Avoids a no-op
  // save call against the backend.
  const dirty =
    username !== user.username ||
    email !== user.email ||
    newPassword.length > 0
  const canSave = currentPassword.length > 0 && dirty

  return (
    <form onSubmit={save} className="border border-border p-4 space-y-4 text-sm">
      <FieldRow label="Username">
        <Input
          value={username}
          onChange={setUsername}
          autoComplete="username"
          autoFocus
        />
      </FieldRow>

      <FieldRow label="Email">
        <Input
          value={email}
          onChange={setEmail}
          type="email"
          autoComplete="email"
        />
      </FieldRow>

      <FieldRow label="Password">
        <Input
          value={newPassword}
          onChange={setNewPassword}
          type="password"
          autoComplete="new-password"
        />
      </FieldRow>

      {error && (
        <div className="text-xs text-destructive font-semibold">{error}</div>
      )}

      {/* Bottom action row — current password is the gate, so it sits
          inline with Save / Cancel rather than as its own labeled row.
          Reads as "type your password and click Save". */}
      <div className="flex items-center gap-2 pt-1">
        <input
          type="password"
          value={currentPassword}
          onChange={(e) => setCurrentPassword(e.target.value)}
          placeholder="Current password"
          autoComplete="current-password"
          spellCheck={false}
          className="flex-1 h-9 bg-transparent border border-border px-3 text-sm font-mono outline-none focus:border-foreground"
        />
        <Button type="submit" disabled={busy || !canSave}>
          {busy ? "Saving…" : "Save"}
        </Button>
        <Button
          type="button"
          variant="outline"
          onClick={cancel}
          disabled={busy}
        >
          Cancel
        </Button>
      </div>
    </form>
  )
}

/**
 * Settings → Account → "Details" box. Read-only account metadata that
 * isn't editable - currently just the join date, split out from the
 * Authentication box above so that box stays focused on credentials.
 */
export function AccountDetails() {
  const { state } = useAuth()
  const user = state.status === "authed" ? state.user : null
  const joined = user?.created_at
    ? new Date(user.created_at).toLocaleDateString(undefined, {
        year: "numeric",
        month: "short",
        day: "numeric",
      })
    : "—"
  return (
    <div className="border border-border p-4 space-y-3 text-sm">
      <Row label="Joined" value={joined} />
    </div>
  )
}

/* Read-only label + value row used in the Authentication view card and
   the Details box. */
function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline gap-3">
      <div className="w-24 shrink-0 text-xs uppercase tracking-wider text-muted-foreground font-semibold">
        {label}
      </div>
      <div className="min-w-0 flex-1 text-foreground font-mono">{value}</div>
    </div>
  )
}

/* Email row variant — same shape as Row but with a "Verify" action
   button on the right when the user hasn't confirmed their address.
   Once they click Verify, button is disabled for an hour-long
   cooldown (matched to the backend's rate limit). The cooldown
   timestamp is persisted in localStorage so a page reload doesn't
   let them spam the send endpoint. */

const VERIFY_COOLDOWN_MS = 60 * 60 * 1000 // 1 hour

function EmailRow({ user }: { user: { id: string; email: string; email_verified: boolean } }) {
  const { toast } = useToast()
  const storageKey = `aether_verify_sent_at_${user.id}`

  const [sentAt, setSentAt] = React.useState<number | null>(() => {
    const v = typeof window !== "undefined" ? localStorage.getItem(storageKey) : null
    return v ? Number(v) : null
  })
  // Server-reported "resend allowed at" timestamp (ms since epoch). The
  // server is the source of truth - localStorage is just an optimistic
  // cache for snappy same-tab updates after sending.
  const [resendAvailableAt, setResendAvailableAt] = React.useState<number | null>(null)
  // tick lets us re-render every 30s so the countdown updates live
  const [, setTick] = React.useState(0)
  const [busy, setBusy] = React.useState(false)

  // On mount (and whenever the user becomes unverified), ask the server
  // when the next resend is allowed. Covers the case where localStorage
  // is missing - fresh signup, new tab, different device, cleared
  // storage - so the button still reflects the real cooldown.
  React.useEffect(() => {
    if (user.email_verified) return
    let cancelled = false
    fetch("/api/auth/verify-cooldown", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((data: { resendAvailableAt: string | null } | null) => {
        if (cancelled || !data) return
        if (data.resendAvailableAt) {
          setResendAvailableAt(Date.parse(data.resendAvailableAt))
        } else {
          setResendAvailableAt(null)
        }
      })
      .catch(() => {
        /* network hiccup - fall back to localStorage-only behavior */
      })
    return () => {
      cancelled = true
    }
  }, [user.email_verified, user.id])

  React.useEffect(() => {
    if (!sentAt && !resendAvailableAt) return
    const id = setInterval(() => setTick((t) => t + 1), 30_000)
    return () => clearInterval(id)
  }, [sentAt, resendAvailableAt])

  // Cooldown remaining = max of (server-reported deadline, localStorage-derived
  // deadline). The server's word wins when it's stricter; localStorage covers
  // the brief window right after a click before the server query catches up.
  const localDeadline = sentAt ? sentAt + VERIFY_COOLDOWN_MS : 0
  const serverDeadline = resendAvailableAt ?? 0
  const deadline = Math.max(localDeadline, serverDeadline)
  const remainingMs = deadline ? Math.max(0, deadline - Date.now()) : 0
  const onCooldown = remainingMs > 0

  const onVerify = async () => {
    if (busy || onCooldown) return
    setBusy(true)
    try {
      const res = await fetch("/api/auth/send-verification", {
        method: "POST",
        credentials: "include",
      })
      if (!res.ok && res.status !== 204) {
        const data = await res.json().catch(() => ({}))
        // 429 means a token was already issued recently — treat the
        // server's word as the source of truth and start the
        // cooldown locally too so the button accurately reflects it.
        if (res.status === 429) {
          const ts = Date.now()
          localStorage.setItem(storageKey, String(ts))
          setSentAt(ts)
        }
        throw new Error(data?.detail || `HTTP ${res.status}`)
      }
      // No success toast — the button dropping into its "Sent · Xh"
      // cooldown state (via setSentAt below) is confirmation enough.
      const ts = Date.now()
      localStorage.setItem(storageKey, String(ts))
      setSentAt(ts)
    } catch (e) {
      toast({
        title: "Couldn't send verification email",
        description: e instanceof Error ? e.message : "Try again later.",
        variant: "error",
      })
    } finally {
      setBusy(false)
    }
  }

  // Reserve space on the right (pr-32) when the Verify button is
  // visible so the email value never runs under it. Button is
  // positioned absolute + center so it doesn't stretch the row's
  // height — keeping every row in the card the same compact size.
  return (
    <div
      className={
        "relative flex items-baseline gap-3 " +
        (user.email_verified ? "" : "pr-32")
      }
    >
      <div
        className={
          "w-24 shrink-0 text-xs uppercase tracking-wider font-semibold " +
          (user.email_verified ? "text-muted-foreground" : "text-destructive")
        }
      >
        Email
      </div>
      <div
        className={
          "min-w-0 flex-1 font-mono " +
          (user.email_verified ? "text-foreground" : "text-destructive")
        }
      >
        {user.email}
      </div>
      {!user.email_verified && (
        <button
          type="button"
          onClick={onVerify}
          disabled={busy || onCooldown}
          className={
            "absolute right-0 top-1/2 -translate-y-1/2 px-3 h-7 text-xs font-bold uppercase tracking-wider border " +
            (onCooldown
              ? "border-border text-muted-foreground cursor-default"
              : "border-destructive text-destructive hover:bg-destructive hover:text-destructive-foreground cursor-pointer")
          }
        >
          {busy
            ? "Sending…"
            : onCooldown
            ? `Sent · ${formatRemaining(remainingMs)}`
            : "Verify"}
        </button>
      )}
    </div>
  )
}

function formatRemaining(ms: number): string {
  const minutes = Math.ceil(ms / 60_000)
  if (minutes >= 60) return "1h"
  return `${minutes}m`
}

/* Editable row — label on the left, input slot on the right. */
function FieldRow({
  label,
  children,
}: {
  label: string
  children: React.ReactNode
}) {
  return (
    <div className="flex items-start gap-3">
      <div className="w-24 shrink-0 text-xs uppercase tracking-wider text-muted-foreground font-semibold pt-2">
        {label}
      </div>
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  )
}

function Input({
  value,
  onChange,
  type = "text",
  placeholder,
  autoFocus,
  autoComplete,
}: {
  value: string
  onChange: (v: string) => void
  type?: string
  placeholder?: string
  autoFocus?: boolean
  autoComplete?: string
}) {
  return (
    <input
      type={type}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      autoFocus={autoFocus}
      autoComplete={autoComplete}
      spellCheck={false}
      className="block w-full bg-transparent border border-border px-3 py-2 text-sm font-mono outline-none focus:border-foreground"
    />
  )
}
