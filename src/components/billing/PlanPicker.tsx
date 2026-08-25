import * as React from "react"
import { useAuth } from "@/auth/AuthContext"
import { useToast } from "@/components/ui/toast"
import { AddCardDialog } from "./AddCardDialog"
import {
  BILLING_CHANGE_EVENT,
  readEffectivePaymentStatus,
  readHasPaymentMethod,
  refreshBillingStatus,
} from "@/lib/paymentStatus"

/**
 * Settings -> Billing plan picker. Shows the commercial tier ladder
 * (Basic -> Creator -> Studio), badges the user's current tier, and lets
 * them switch between the wired tiers.
 *
 * Display source of truth mirrors billing.py
 * (STORAGE_PRICE_PER_GB_MONTH_BY_TIER) + the subscription amounts.
 * Basic + Creator are purchasable (each has a Stripe price + the
 * /api/billing/plan switch endpoint); Studio stays "Coming soon"
 * (available: false) until its enterprise checkout exists.
 *
 * Switching is a clean cancel + re-subscribe on the backend (no
 * proration). A user with no card on file gets the add-card dialog
 * pre-targeted at the chosen tier so they enroll straight into it.
 */

type Plan = {
  id: string
  name: string
  price: string
  cadence: string
  storage: string
  available: boolean
}

const PLANS: Plan[] = [
  {
    id: "basic",
    name: "Basic",
    price: "$1",
    cadence: "/ year",
    storage: "$0.02 / GB-mo",
    available: true,
  },
  {
    id: "creator",
    name: "Creator",
    price: "$25",
    cadence: "/ month",
    storage: "$0.01 / GB-mo",
    available: true,
  },
  {
    id: "studio",
    name: "Studio",
    price: "$2,500",
    cadence: "/ month",
    storage: "$0.0075 / GB-mo",
    available: false,
  },
]

function cap(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1)
}

export function PlanPicker() {
  const { state, refresh } = useAuth()
  const { toast } = useToast()
  const currentTier =
    state.status === "authed" ? state.user.effective_tier : null

  // Two distinct facts, now decoupled:
  //   onPlan  - has an active subscription. Drives the "Current" badge
  //             and the Switch-vs-Choose label.
  //   hasCard - a card is on file (independent of plan). Drives whether
  //             a click subscribes directly or opens the add-card dialog.
  const [paymentStatus, setPaymentStatus] = React.useState<string | null>(() =>
    readEffectivePaymentStatus()
  )
  const [hasCard, setHasCard] = React.useState<boolean>(() =>
    readHasPaymentMethod()
  )
  React.useEffect(() => {
    const sync = () => {
      setPaymentStatus(readEffectivePaymentStatus())
      setHasCard(readHasPaymentMethod())
    }
    window.addEventListener(BILLING_CHANGE_EVENT, sync)
    window.addEventListener("storage", sync)
    void refreshBillingStatus()
    return () => {
      window.removeEventListener(BILLING_CHANGE_EVENT, sync)
      window.removeEventListener("storage", sync)
    }
  }, [])
  const onPlan = paymentStatus === "active" || paymentStatus === "past_due"

  const [busyTier, setBusyTier] = React.useState<string | null>(null)
  const [addCardTier, setAddCardTier] = React.useState<string | null>(null)

  const switchPlan = async (tier: string) => {
    if (busyTier) return
    setBusyTier(tier)
    try {
      const res = await fetch("/api/billing/plan", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tier }),
      })
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}))
        throw new Error(detail?.detail || `HTTP ${res.status}`)
      }
      await refresh() // pick up the new tier on the user object
      void refreshBillingStatus() // refresh the shared billing cache
      toast({ title: `You're on ${cap(tier)} now` })
    } catch (e) {
      toast({
        title: "Couldn't switch plans",
        description: e instanceof Error ? e.message : "",
        variant: "error",
      })
    } finally {
      setBusyTier(null)
    }
  }

  const onSelect = (tier: string) => {
    if (hasCard) void switchPlan(tier)
    else setAddCardTier(tier)
  }

  return (
    <>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {PLANS.map((p) => {
          const isCurrent = onPlan && p.id === currentTier
          return (
            <div
              key={p.id}
              className={
                "flex flex-col border p-4 " +
                (isCurrent
                  ? "border-foreground"
                  : p.available
                  ? "border-border"
                  : "border-border opacity-50")
              }
            >
              <div className="flex items-center justify-between gap-2">
                <div className="text-sm font-bold">{p.name}</div>
                {isCurrent && (
                  <span className="text-[10px] uppercase tracking-wider font-bold border border-foreground/40 px-1.5 py-0.5">
                    Current
                  </span>
                )}
              </div>

              <div className="mt-3 flex items-baseline gap-1">
                <span className="text-xl font-bold font-mono tabular-nums">
                  {p.price}
                </span>
                <span className="text-xs text-muted-foreground">
                  {p.cadence}
                </span>
              </div>
              <div className="mt-1 text-xs font-mono text-muted-foreground">
                {p.storage}
              </div>

              <div className="mt-auto pt-4">
                {isCurrent ? (
                  <div className="text-xs text-muted-foreground">Your plan</div>
                ) : p.available ? (
                  <button
                    type="button"
                    disabled={busyTier !== null}
                    onClick={() => onSelect(p.id)}
                    className="w-full cursor-pointer border border-foreground px-3 py-2 text-xs font-bold uppercase tracking-wider hover:bg-foreground hover:text-background disabled:cursor-default disabled:opacity-50 disabled:hover:bg-transparent disabled:hover:text-foreground"
                  >
                    {busyTier === p.id
                      ? "Working…"
                      : onPlan
                      ? "Switch"
                      : "Choose"}
                  </button>
                ) : (
                  <div className="border border-border px-2 py-1.5 text-center text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
                    Coming soon
                  </div>
                )}
              </div>
            </div>
          )
        })}
      </div>

      <AddCardDialog
        open={addCardTier !== null}
        tier={addCardTier ?? undefined}
        onClose={() => setAddCardTier(null)}
        onSuccess={() => {
          const t = addCardTier
          setAddCardTier(null)
          void refresh()
          void refreshBillingStatus()
          if (t) toast({ title: `You're on ${cap(t)} now` })
        }}
      />
    </>
  )
}
