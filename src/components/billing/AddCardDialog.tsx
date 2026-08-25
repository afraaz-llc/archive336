import * as React from "react"
import { loadStripe, type Stripe } from "@stripe/stripe-js"
import {
  Elements,
  PaymentElement,
  useElements,
  useStripe,
} from "@stripe/react-stripe-js"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"

/**
 * Add-a-card flow built on Stripe SetupIntent + Elements.
 *
 * 1. Open: ask backend for a SetupIntent (creates the Stripe Customer if
 *    needed) → it returns clientSecret + publishableKey.
 * 2. Mount <Elements> with a dark "night" appearance to match our pitch
 *    black UI.
 * 3. On submit: stripe.confirmSetup({ redirect: 'if_required' }). For US
 *    cards without 3DS this resolves inline; for cards that need 3DS
 *    Stripe shows its own modal.
 * 4. On success: POST /api/billing/setup-confirm to flip our local
 *    payment_status='active' immediately (instead of waiting on the
 *    webhook to round-trip). Then onSuccess() so the parent re-fetches.
 */

// loadStripe is async + heavy. Cache by publishable key so we don't load
// the SDK twice in the same session.
const _stripeCache = new Map<string, Promise<Stripe | null>>()
function getStripe(pk: string) {
  let p = _stripeCache.get(pk)
  if (!p) {
    p = loadStripe(pk)
    _stripeCache.set(pk, p)
  }
  return p
}

// Map Stripe's payment-method type id → the verb on our submit button.
// Anything we don't have specific copy for (Link, wallets, etc.) gets
// the generic "Save payment method" so it never lies about what's
// being saved.
function saveLabelFor(type: string | null): string {
  switch (type) {
    case "card":
      return "Save card"
    case "us_bank_account":
      return "Save bank account"
    case null:
    case undefined:
      return "Save"
    default:
      return "Save payment method"
  }
}

// Keep this lean. Stripe rejects unsupported CSS in `rules` and silently
// fails the entire loader (the iframe collapses to 1px) — variables only
// is the safest contract. Theme 'night' gives us the dark base; the rest
// is just brand color tokens.
const APPEARANCE = {
  theme: "night" as const,
  variables: {
    colorPrimary: "#ffffff",
    colorBackground: "#000000",
    colorText: "#ffffff",
    colorDanger: "#ef4444",
    fontFamily: '"Nunito", system-ui, sans-serif',
    borderRadius: "0px",
  },
}

type Props = {
  open: boolean
  onClose: () => void
  onSuccess: () => void
  /** When set, the card is enrolled directly into this plan tier via
   *  /api/billing/plan (used by the plan picker). When omitted, defaults
   *  to the $1/yr Basic membership via /api/billing/setup-confirm. */
  tier?: string
}

export function AddCardDialog({ open, onClose, onSuccess, tier }: Props) {
  const [clientSecret, setClientSecret] = React.useState<string | null>(null)
  const [publishableKey, setPublishableKey] = React.useState<string | null>(null)
  const [error, setError] = React.useState<string | null>(null)
  const [stripePromise, setStripePromise] =
    React.useState<Promise<Stripe | null> | null>(null)

  // Reset state every time the dialog opens. We always grab a fresh
  // SetupIntent because Stripe's client_secret is single-use.
  React.useEffect(() => {
    if (!open) {
      setClientSecret(null)
      setError(null)
      return
    }
    let cancelled = false
    ;(async () => {
      try {
        const res = await fetch("/api/billing/setup-intent", {
          method: "POST",
          credentials: "include",
        })
        if (!res.ok) {
          const detail = await res.json().catch(() => ({}))
          throw new Error(detail?.detail || `HTTP ${res.status}`)
        }
        const data = (await res.json()) as {
          clientSecret: string
          publishableKey: string
        }
        if (cancelled) return
        if (!data.publishableKey) {
          throw new Error("Stripe publishable key missing on server.")
        }
        setClientSecret(data.clientSecret)
        setPublishableKey(data.publishableKey)
        setStripePromise(getStripe(data.publishableKey))
      } catch (e) {
        if (cancelled) return
        setError(e instanceof Error ? e.message : "Couldn't start setup.")
      }
    })()
    return () => {
      cancelled = true
    }
  }, [open])

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-md">
        {/* Cap the dialog height so it never overruns the viewport — Stripe's
            full PaymentElement (with Link / bank-account flows expanded) is
            taller than a phone screen. The inner div scrolls within. */}
        <div className="p-6 space-y-5 overflow-y-auto max-h-[calc(100vh-4rem)]">
          <DialogHeader>
            <DialogTitle>Add payment method</DialogTitle>
            {tier && (
              <DialogDescription className="text-xs leading-relaxed">
                {tier === "creator"
                  ? "Adding your payment method starts your $25/month Creator plan. $25 is charged today and renews monthly. Storage usage is billed separately at the Creator rate. Cancel or switch anytime from Settings."
                  : "Adding your payment method starts your $1/year Basic membership. $1 is charged today and renews automatically on this date each year. Storage usage is billed separately. Cancel or switch anytime from Settings."}
              </DialogDescription>
            )}
          </DialogHeader>

          {error && (
            <div className="border border-destructive p-3 text-xs text-destructive">
              {error}
            </div>
          )}

          {!clientSecret && !error && (
            <div className="text-sm text-muted-foreground">Loading…</div>
          )}

          {clientSecret && stripePromise && publishableKey && (
            <Elements
              stripe={stripePromise}
              options={{ clientSecret, appearance: APPEARANCE }}
              key={clientSecret /* re-mount per fresh secret */}
            >
              <CardForm tier={tier} onSuccess={onSuccess} onCancel={onClose} />
            </Elements>
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}

function CardForm({
  tier,
  onSuccess,
  onCancel,
}: {
  tier?: string
  onSuccess: () => void
  onCancel: () => void
}) {
  const stripe = useStripe()
  const elements = useElements()
  const [submitting, setSubmitting] = React.useState(false)
  const [formError, setFormError] = React.useState<string | null>(null)
  // Track which payment method tab is active so the submit button can
  // mirror it ("Save card" vs "Save bank account" vs generic "Save"
  // for Link / wallets where the user picks the inner instrument).
  const [methodType, setMethodType] = React.useState<string | null>(null)

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!stripe || !elements || submitting) return
    setSubmitting(true)
    setFormError(null)
    try {
      const result = await stripe.confirmSetup({
        elements,
        // We're a SPA — handle the success in-page rather than redirect.
        // Stripe still does its own redirect for 3DS challenges.
        confirmParams: {
          return_url: window.location.origin + "/settings",
        },
        redirect: "if_required",
      })
      if (result.error) {
        setFormError(result.error.message || "Card couldn't be saved.")
        return
      }
      // The card is now attached to the customer. If a plan was chosen
      // (from the picker), subscribe to it. Otherwise this is attach-only
      // — save the card now, pick a plan later — and there's nothing more
      // to call: adding a card no longer starts a subscription.
      if (tier) {
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
      }
      onSuccess()
    } catch (e) {
      setFormError(e instanceof Error ? e.message : "Something went wrong.")
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={onSubmit} className="space-y-4">
      <PaymentElement
        options={{
          // Restrict the payment-method tabs to just card. Link and
          // bank-account flows don't help us as a single-merchant
          // SaaS and their wallets persist UI on the page after the
          // dialog closes. Combined with disabling Link in the
          // Stripe Dashboard (Settings → Payments → Link), the
          // floating launcher in the page corner goes away.
          wallets: { applePay: "never", googlePay: "never" },
          paymentMethodOrder: ["card"],
        }}
        onChange={(event) => {
          // event.value.type is the active method ('card', etc.).
          // We only use it to label the submit button.
          setMethodType(event.value?.type ?? null)
        }}
        onLoadError={(event) => {
          // event.error has { type, code, message }. Log enough context
          // to debug Stripe rejecting the load — usually it's an option
          // shape they don't accept silently.
          // eslint-disable-next-line no-console
          console.error("[stripe loaderror]", JSON.stringify(event?.error))
          setFormError(
            event?.error?.message || "Couldn't load the payment form."
          )
        }}
      />
      {formError && (
        <div className="border border-destructive p-3 text-xs text-destructive">
          {formError}
        </div>
      )}
      <div className="flex items-center justify-end gap-2 pt-2">
        <Button type="button" variant="outline" onClick={onCancel}>
          Cancel
        </Button>
        <Button type="submit" disabled={!stripe || !elements || submitting}>
          {submitting ? "Saving…" : saveLabelFor(methodType)}
        </Button>
      </div>
    </form>
  )
}
