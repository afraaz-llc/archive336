import * as React from "react"
import { CreditCard, Landmark } from "lucide-react"
import { Button } from "@/components/ui/button"
import { useToast } from "@/components/ui/toast"
import { AddCardDialog } from "./AddCardDialog"
import {
  BILLING_CHANGE_EVENT,
  readHasPaymentMethod,
  readPaymentMethod,
  refreshBillingStatus,
  type PaymentMethod,
} from "@/lib/paymentStatus"

const CARD_BRAND_LABELS: Record<string, string> = {
  visa: "Visa",
  mastercard: "Mastercard",
  amex: "Amex",
  discover: "Discover",
  diners: "Diners Club",
  jcb: "JCB",
  unionpay: "UnionPay",
}

// Turn the raw payment method into a display title/subtitle + which icon
// to show. Returns null when there's no usable detail, so the caller falls
// back to the generic "Payment method on file" label.
function formatMethod(
  pm: PaymentMethod | null
): { title: string; sub: string; kind: "card" | "bank" } | null {
  if (!pm) return null

  if (pm.type === "us_bank_account") {
    const name = pm.bankName || "Bank account"
    return {
      title: pm.last4 ? `${name} •••• ${pm.last4}` : name,
      sub: "Bank account — used automatically when you subscribe to a plan.",
      kind: "bank",
    }
  }

  if (pm.type === "card") {
    if (!pm.last4) return null
    const brand = pm.brand
      ? CARD_BRAND_LABELS[pm.brand] ??
        pm.brand.charAt(0).toUpperCase() + pm.brand.slice(1)
      : "Card"
    const sub =
      pm.expMonth && pm.expYear
        ? `Expires ${String(pm.expMonth).padStart(2, "0")}/${String(pm.expYear).slice(-2)}`
        : "Used automatically when you subscribe to a plan."
    return { title: `${brand} •••• ${pm.last4}`, sub, kind: "card" }
  }

  // Any other Stripe type (sepa_debit, link, …).
  if (pm.last4) {
    return {
      title: `Payment method •••• ${pm.last4}`,
      sub: "Used automatically when you subscribe to a plan.",
      kind: "card",
    }
  }
  return null
}

/**
 * Settings → Billing → "Payment method" section.
 *
 * Account-level payment management, decoupled from plan selection: a user
 * can save a method here (no charge) before picking a plan. The in-app add
 * flow is card-only, but a user can attach an ACH bank account via the
 * Stripe "Manage" portal — so this renders cards AND bank accounts. Shows
 * the method on file + Manage once one is saved, or an Add button otherwise.
 */
export function PaymentMethodSection({
  alert = false,
}: {
  // When true (no payment method on file + billing is known), render the
  // whole row red as a "you need to add a card" alert. Driven from
  // Settings so the row and its container border stay in lockstep.
  alert?: boolean
}) {
  const { toast } = useToast()
  const [hasCard, setHasCard] = React.useState<boolean>(() =>
    readHasPaymentMethod()
  )
  const [method, setMethod] = React.useState<PaymentMethod | null>(() =>
    readPaymentMethod()
  )
  React.useEffect(() => {
    const sync = () => {
      setHasCard(readHasPaymentMethod())
      setMethod(readPaymentMethod())
    }
    window.addEventListener(BILLING_CHANGE_EVENT, sync)
    window.addEventListener("storage", sync)
    void refreshBillingStatus()
    return () => {
      window.removeEventListener(BILLING_CHANGE_EVENT, sync)
      window.removeEventListener("storage", sync)
    }
  }, [])

  const [openAdd, setOpenAdd] = React.useState(false)
  const [busy, setBusy] = React.useState(false)

  const openPortal = async () => {
    setBusy(true)
    try {
      const res = await fetch("/api/billing/portal", { credentials: "include" })
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}))
        throw new Error(detail?.detail || `HTTP ${res.status}`)
      }
      const data = (await res.json()) as { url: string }
      window.location.href = data.url
    } catch (e) {
      toast({
        title: "Couldn't open the billing portal",
        description: e instanceof Error ? e.message : "Unknown error",
        variant: "error",
      })
      setBusy(false)
    }
  }

  const info = formatMethod(method)
  const Icon = info?.kind === "bank" ? Landmark : CreditCard

  return (
    <div className="flex items-center gap-4 p-4">
      <div
        className={`size-10 shrink-0 border flex items-center justify-center ${
          alert ? "border-destructive" : "border-border"
        }`}
      >
        <Icon
          className={`size-5 ${
            alert ? "text-destructive" : "text-muted-foreground"
          }`}
        />
      </div>
      <div className="min-w-0 flex-1">
        <div
          className={`text-sm font-semibold ${alert ? "text-destructive" : ""}`}
        >
          {info?.title ??
            (hasCard ? "Payment method on file" : "No payment method")}
        </div>
        <div
          className={`text-xs mt-0.5 ${
            alert ? "text-destructive/80" : "text-muted-foreground"
          }`}
        >
          {info?.sub ??
            (hasCard
              ? "Used automatically when you subscribe to a plan."
              : "Add a card now and pick a plan whenever you're ready — no charge to save it.")}
        </div>
      </div>
      <div className="shrink-0">
        {hasCard ? (
          <Button variant="outline" onClick={openPortal} disabled={busy}>
            Manage payment method
          </Button>
        ) : (
          <Button
            variant={alert ? "outline" : "default"}
            onClick={() => setOpenAdd(true)}
            className={
              alert
                ? "border-destructive text-destructive hover:bg-destructive hover:text-destructive-foreground"
                : undefined
            }
          >
            Add payment method
          </Button>
        )}
      </div>
      <AddCardDialog
        open={openAdd}
        onClose={() => setOpenAdd(false)}
        onSuccess={() => {
          setOpenAdd(false)
          void refreshBillingStatus()
          toast({ title: "Card saved" })
        }}
      />
    </div>
  )
}
