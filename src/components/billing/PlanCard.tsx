import * as React from "react"
import { AlertTriangle } from "lucide-react"
import { BILLING_CHANGE_EVENT, getDevPaymentOverride } from "@/lib/paymentStatus"

/**
 * Settings → Billing → "Usage & billing" card.
 *
 * Only rendered once the user is on a plan (active / past_due). Shows the
 * real numbers — storage used, accrued toward the next bill, estimated
 * bill — plus a past-due warning. Card management lives in the separate
 * "Payment method" section above, so there's deliberately no manage
 * button here.
 */

type Status = {
  paymentStatus: "none" | "active" | "past_due" | "canceled"
  stripeCustomerId: string | null
  currentBytes: number
  currentGb: number
  unbilledUsd: number
  monthlyEstimateUsd: number
  billThresholdUsd: number
  pricePerGbPerMonthUsd: number
  annualFeeUsd: number
  billingDayOfMonth: number
  lastBilledAt: string | null
}

// Cache the most recent /status response so a refresh paints immediately
// instead of flashing "Loading…". Cleared on 401 so a logged-out browser
// doesn't keep showing a stale card.
const STATUS_CACHE_KEY = "archive336_billing_status_cache"

function readCachedStatus(): Status | null {
  if (typeof window === "undefined") return null
  try {
    const v = localStorage.getItem(STATUS_CACHE_KEY)
    return v ? (JSON.parse(v) as Status) : null
  } catch {
    return null
  }
}

function writeCachedStatus(s: Status | null): void {
  if (typeof window === "undefined") return
  try {
    if (s === null) localStorage.removeItem(STATUS_CACHE_KEY)
    else localStorage.setItem(STATUS_CACHE_KEY, JSON.stringify(s))
  } catch {
    /* private mode / quota — ignore */
  }
  try {
    window.dispatchEvent(new Event("archive336-billing-status-changed"))
  } catch {
    /* ignore (older browsers / SSR) */
  }
}

export function PlanCard() {
  const [status, setStatus] = React.useState<Status | null>(() =>
    readCachedStatus()
  )
  // Re-render when the dev override flips so the past-due treatment can be
  // previewed without a real Stripe round-trip.
  const [, forceTick] = React.useState(0)
  React.useEffect(() => {
    const sync = () => forceTick((n) => n + 1)
    window.addEventListener(BILLING_CHANGE_EVENT, sync)
    window.addEventListener("storage", sync)
    return () => {
      window.removeEventListener(BILLING_CHANGE_EVENT, sync)
      window.removeEventListener("storage", sync)
    }
  }, [])

  const refresh = React.useCallback(async () => {
    try {
      const res = await fetch("/api/billing/status", { credentials: "include" })
      if (!res.ok) {
        if (res.status === 401) {
          writeCachedStatus(null)
          setStatus(null)
        }
        return
      }
      const data = (await res.json()) as Status
      setStatus(data)
      writeCachedStatus(data)
    } catch {
      /* transient network error — leave cached value showing */
    }
  }, [])

  React.useEffect(() => {
    void refresh()
  }, [refresh])

  if (status === null) {
    return (
      <div className="border border-border p-4 text-sm text-muted-foreground">
        Loading…
      </div>
    )
  }

  const override = getDevPaymentOverride()
  const effectivePaymentStatus = (override ??
    status.paymentStatus) as Status["paymentStatus"]
  const pastDue = effectivePaymentStatus === "past_due"

  return (
    <div
      className={
        "border p-4 space-y-4 " +
        (pastDue ? "border-destructive" : "border-border")
      }
    >
      {pastDue && (
        <div className="flex items-start gap-3 border border-destructive bg-destructive/5 p-3 text-xs leading-relaxed">
          <AlertTriangle className="size-4 shrink-0 mt-0.5" />
          <div>
            <div className="font-semibold">Last bill couldn't be charged</div>
            <p className="text-muted-foreground mt-0.5">
              Update your card in Payment method above to keep syncing.
              Existing archives stay on our servers — nothing has been
              deleted.
            </p>
          </div>
        </div>
      )}

      <div className="grid grid-cols-3 gap-px bg-border border border-border">
        <Stat label="Stored now" value={`${status.currentGb.toFixed(2)} GB`} />
        <Stat
          label="Accrued this period"
          value={`$${status.unbilledUsd.toFixed(2)}`}
          hint={`of $${status.billThresholdUsd.toFixed(2)} threshold`}
        />
        <Stat
          label="Estimated next bill"
          value={`$${status.monthlyEstimateUsd.toFixed(2)}`}
          hint="if storage stays flat"
        />
      </div>

    </div>
  )
}

function Stat({
  label,
  value,
  hint,
}: {
  label: string
  value: string
  hint?: string
}) {
  return (
    <div className="bg-card p-3">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold">
        {label}
      </div>
      <div className="text-base font-semibold mt-1">{value}</div>
      {hint && (
        <div className="text-[10px] text-muted-foreground mt-0.5">{hint}</div>
      )}
    </div>
  )
}
