import * as React from "react"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { useToast } from "@/components/ui/toast"
import { useAuth } from "@/auth/AuthContext"
import { refreshBillingStatus } from "@/lib/paymentStatus"

/**
 * Settings → Billing → cancel action. Shown only on an active plan.
 *
 * POST /billing/cancel stops the membership charge and moves the archive
 * into the same 30-day grace window a disconnect uses (kept, not billed).
 * Resubscribing within the window restores everything and settles the
 * storage held during the pause; after 30 days it's removed.
 */
export function CancelPlanSection() {
  const { toast } = useToast()
  const { state } = useAuth()
  const tier = state.status === "authed" ? state.user.effective_tier : null
  const planName = tier
    ? tier.charAt(0).toUpperCase() + tier.slice(1)
    : "Your plan"
  const [open, setOpen] = React.useState(false)
  const [busy, setBusy] = React.useState(false)

  const cancel = async () => {
    setBusy(true)
    try {
      const res = await fetch("/api/billing/cancel", {
        method: "POST",
        credentials: "include",
      })
      if (!res.ok) {
        const d = await res.json().catch(() => ({}))
        throw new Error(d?.detail || `HTTP ${res.status}`)
      }
      await refreshBillingStatus()
      setOpen(false)
      toast({
        title: "Plan canceled",
        description: "Your archive is kept for 30 days if you change your mind.",
      })
    } catch (e) {
      toast({
        title: "Couldn't cancel your plan",
        description: e instanceof Error ? e.message : "Please try again.",
        variant: "error",
      })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex items-center justify-between p-4">
      <div className="min-w-0 pr-4">
        <div className="text-sm font-semibold">{planName}</div>
      </div>
      <Button
        variant="outline"
        onClick={() => setOpen(true)}
        className="shrink-0 border-destructive text-destructive hover:bg-destructive hover:text-destructive-foreground"
      >
        Cancel plan
      </Button>

      <Dialog open={open} onOpenChange={(o) => !busy && setOpen(o)}>
        <DialogContent className="max-w-md">
          <div className="p-6 space-y-5">
            <DialogHeader>
              <DialogTitle>Cancel your plan?</DialogTitle>
            </DialogHeader>
            <p className="text-sm text-muted-foreground leading-relaxed">
              Your plan ends and billing stops now. Your archive is kept for{" "}
              <strong>30 days</strong> in case you change your mind — resubscribe
              within that window and everything is restored (you'll just settle
              the storage we held for you during the pause). After 30 days it's
              removed.
            </p>
            <DialogFooter>
              <Button
                variant="outline"
                onClick={() => setOpen(false)}
                disabled={busy}
              >
                Keep my plan
              </Button>
              <Button variant="destructive" onClick={cancel} disabled={busy}>
                {busy ? "…" : "Cancel plan"}
              </Button>
            </DialogFooter>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}
