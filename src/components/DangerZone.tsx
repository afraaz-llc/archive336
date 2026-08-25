import * as React from "react"
import { Download, Mail, Trash2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { useAuth } from "@/auth/AuthContext"
import { useToast } from "@/components/ui/toast"

type OutstandingCharge = {
  storageUsd: number
  feeUsd: number
  totalUsd: number
  hasCard: boolean
}

/**
 * Bottom-of-Settings section. Three rows: Export, Delete, Legal.
 *
 * Delete flow is two-step: the dialog charges the card + sends a
 * verification email, then the user clicks the email link to
 * actually wipe the account. The dialog flips to a 'check your
 * email' phase after a successful submit. On card decline / Stripe
 * outage the dialog stays open so the user can fix and retry.
 */
export function DangerZone() {
  const { state } = useAuth()
  const userEmail = state.status === "authed" ? state.user.email : ""
  const { toast } = useToast()

  const [exporting, setExporting] = React.useState(false)
  const [confirmOpen, setConfirmOpen] = React.useState(false)
  const [phase, setPhase] = React.useState<"collect" | "sent">("collect")
  const [password, setPassword] = React.useState("")
  const [exportRequested, setExportRequested] = React.useState(false)
  const [submitting, setSubmitting] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)

  // Charge breakdown - fetched when the dialog opens.
  const [charge, setCharge] = React.useState<OutstandingCharge | null>(null)
  const [chargeError, setChargeError] = React.useState<string | null>(null)

  React.useEffect(() => {
    if (!confirmOpen) {
      setPhase("collect")
      setPassword("")
      setExportRequested(false)
      setSubmitting(false)
      setError(null)
      setCharge(null)
      setChargeError(null)
      return
    }
    let cancelled = false
    void (async () => {
      try {
        const r = await fetch("/api/auth/me/outstanding-charge", {
          credentials: "include",
        })
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        const data = (await r.json()) as OutstandingCharge
        if (!cancelled) setCharge(data)
      } catch (e) {
        if (!cancelled)
          setChargeError(e instanceof Error ? e.message : "Failed")
      }
    })()
    return () => {
      cancelled = true
    }
  }, [confirmOpen])

  const handleExport = async () => {
    setExporting(true)
    try {
      const res = await fetch("/api/auth/me/export", {
        credentials: "include",
      })
      if (res.status === 429) {
        // Rate-limited — surface the wait time from the server detail.
        const detail = await res.json().catch(() => ({}))
        toast({
          title: "Already exported recently",
          description:
            detail?.detail ||
            "Exports are limited to once per hour. Try again later.",
          variant: "error",
          duration: 30000,
        })
        return
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      // Response is NDJSON (one JSON object per line). The client just
      // saves it as-is - no parsing needed. blob() streams the body
      // under the hood so memory stays bounded even on big exports.
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement("a")
      const date = new Date().toISOString().split("T")[0]
      a.href = url
      a.download = `archive336-export-${date}.ndjson`
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    } catch (e) {
      toast({
        title: "Export failed",
        description: e instanceof Error ? e.message : "Unknown error",
        variant: "error",
        duration: 30000,
      })
    } finally {
      setExporting(false)
    }
  }

  const handleRequestDelete = async () => {
    setSubmitting(true)
    setError(null)
    try {
      const res = await fetch("/api/auth/me/request-delete", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          current_password: password,
          export_requested: exportRequested,
        }),
      })
      if (res.status === 403) {
        setError("That password is incorrect.")
        return
      }
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}))
        throw new Error(detail?.detail || `HTTP ${res.status}`)
      }
      // Card charged + email sent. Flip to the check-your-email phase.
      setPhase("sent")
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't send confirmation.")
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <>
      <div className="border border-border p-4 space-y-3">
        <div className="flex items-center justify-between gap-4">
          <div className="text-sm font-semibold">Export my data</div>
          <Button
            variant="outline"
            onClick={() => void handleExport()}
            disabled={exporting}
          >
            <Download />
            {exporting ? "Exporting…" : "Download JSON"}
          </Button>
        </div>

        <div className="border-t border-border" />

        <div className="flex items-center justify-between gap-4">
          <div className="text-sm font-semibold">Delete my account</div>
          <Button
            variant="destructive"
            onClick={() => setConfirmOpen(true)}
          >
            <Trash2 />
            Delete account
          </Button>
        </div>

        <div className="border-t border-border" />

        <div className="flex items-center justify-between gap-4 flex-wrap">
          <div className="text-sm font-semibold">Legal</div>
          <div className="flex items-center gap-x-4 gap-y-2 text-xs flex-wrap">
            <a
              href="/terms"
              target="_blank"
              rel="noopener noreferrer"
              className="text-muted-foreground hover:text-foreground"
            >
              Terms of Service
            </a>
            <span className="text-muted-foreground">·</span>
            <a
              href="/privacy"
              target="_blank"
              rel="noopener noreferrer"
              className="text-muted-foreground hover:text-foreground"
            >
              Privacy Policy
            </a>
          </div>
        </div>
      </div>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent className="max-w-md">
          <div className="p-6 space-y-5">
            {phase === "collect" ? (
              <>
                <DialogHeader>
                  <DialogTitle>Delete account</DialogTitle>
                </DialogHeader>

                <ChargeBreakdown charge={charge} error={chargeError} />

                <div>
                  <label
                    htmlFor="dz-pw"
                    className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold block mb-1"
                  >
                    Confirm with password
                  </label>
                  <Input
                    id="dz-pw"
                    type="password"
                    placeholder="Current password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    disabled={submitting}
                    autoFocus
                  />
                </div>

                <label className="flex items-start gap-3 cursor-pointer text-sm">
                  <span className="mt-0.5">
                    <Checkbox
                      checked={exportRequested}
                      onCheckedChange={setExportRequested}
                      disabled={submitting}
                      aria-label="Email a copy of my data"
                    />
                  </span>
                  <span>Email a copy of my data</span>
                </label>

                {error && <p className="text-xs text-destructive">{error}</p>}

                <DialogFooter>
                  <Button
                    variant="ghost"
                    onClick={() => setConfirmOpen(false)}
                    disabled={submitting}
                  >
                    Cancel
                  </Button>
                  <Button
                    variant="destructive"
                    onClick={() => void handleRequestDelete()}
                    disabled={!password || submitting || charge === null}
                  >
                    {submitting ? "Sending…" : "DELETE"}
                  </Button>
                </DialogFooter>
              </>
            ) : (
              <>
                <DialogHeader>
                  <DialogTitle>Check your email</DialogTitle>
                </DialogHeader>
                <div className="flex items-start gap-3 p-3 border border-border bg-muted/20">
                  <Mail className="size-5 mt-0.5 shrink-0" />
                  <div>
                    <div className="font-semibold text-sm">{userEmail}</div>
                    <p className="text-xs text-muted-foreground mt-1 leading-relaxed">
                      Email valid for 1 hour
                    </p>
                  </div>
                </div>
                <DialogFooter>
                  <Button onClick={() => setConfirmOpen(false)}>Close</Button>
                </DialogFooter>
              </>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </>
  )
}

function ChargeBreakdown({
  charge,
  error,
}: {
  charge: OutstandingCharge | null
  error: string | null
}) {
  if (error) {
    return (
      <p className="text-xs text-destructive">
        Couldn't compute the final charge: {error}
      </p>
    )
  }
  if (charge === null) {
    return (
      <p className="text-xs text-muted-foreground">Computing final charge…</p>
    )
  }
  if (charge.totalUsd <= 0) {
    return (
      <div className="border border-border p-3 text-xs">
        <div className="font-semibold">No outstanding balance</div>
        <p className="text-muted-foreground mt-1">
          You won't be charged anything.
        </p>
      </div>
    )
  }
  return (
    <div className="border border-border p-3 text-xs space-y-1">
      <div className="font-semibold">Final charge</div>
      <div className="flex justify-between">
        <span className="text-muted-foreground">Outstanding storage</span>
        <span className="font-mono tabular-nums">
          ${charge.storageUsd.toFixed(2)}
        </span>
      </div>
      {charge.feeUsd > 0 && (
        <div className="flex justify-between">
          <span className="text-muted-foreground">Transaction fee</span>
          <span className="font-mono tabular-nums">
            ${charge.feeUsd.toFixed(2)}
          </span>
        </div>
      )}
      <div className="flex justify-between border-t border-border pt-1 mt-1">
        <span className="font-semibold">Total</span>
        <span className="font-mono tabular-nums font-semibold">
          ${charge.totalUsd.toFixed(2)}
        </span>
      </div>
      {!charge.hasCard && (
        <p className="text-destructive mt-2">
          No card on file. Add a payment method first or this will fail.
        </p>
      )}
    </div>
  )
}
