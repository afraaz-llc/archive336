/**
 * Shared cached payment status used across Sidebar, Settings, PlanCard,
 * and any Paywalled wrappers.
 *
 * Two layers:
 *   1. Real cache  — `archive336_billing_status_cache` written by PlanCard
 *      after each /api/billing/status fetch. Source of truth for normal use.
 *   2. Dev override — `archive336_dev_payment_override`. When set ("active" |
 *      "none" | "past_due" | …), all consumers see this value instead of the
 *      real one. Wired up via the floating DevPaymentToggle (admin only) so
 *      the no-card / has-card UX can be iterated on without actually
 *      adding/removing a card in Stripe.
 *
 * Both layers fire the same `archive336-billing-status-changed` event so any
 * subscribed component re-renders immediately on change.
 */

export const BILLING_CACHE_KEY = "archive336_billing_status_cache"
export const DEV_OVERRIDE_KEY = "archive336_dev_payment_override"
export const BILLING_CHANGE_EVENT = "archive336-billing-status-changed"

// Separate from DEV_OVERRIDE_KEY: this just controls whether the
// floating payment-simulator pill is rendered in the bottom-right.
// The override itself can still be set even while the pill is hidden.
export const DEV_OVERLAY_VISIBLE_KEY = "archive336_dev_overlay_visible"
export const DEV_OVERLAY_CHANGE_EVENT = "archive336-dev-overlay-changed"

export function getDevOverlayVisible(): boolean {
  if (typeof window === "undefined") return false
  try {
    return localStorage.getItem(DEV_OVERLAY_VISIBLE_KEY) === "1"
  } catch {
    return false
  }
}

export function setDevOverlayVisible(v: boolean): void {
  if (typeof window === "undefined") return
  try {
    if (v) localStorage.setItem(DEV_OVERLAY_VISIBLE_KEY, "1")
    else localStorage.removeItem(DEV_OVERLAY_VISIBLE_KEY)
    window.dispatchEvent(new CustomEvent(DEV_OVERLAY_CHANGE_EVENT))
  } catch {
    /* private mode / quota - ignore */
  }
}

export function getDevPaymentOverride(): string | null {
  if (typeof window === "undefined") return null
  try {
    return localStorage.getItem(DEV_OVERRIDE_KEY)
  } catch {
    return null
  }
}

export function setDevPaymentOverride(status: string | null): void {
  if (typeof window === "undefined") return
  try {
    if (status === null) localStorage.removeItem(DEV_OVERRIDE_KEY)
    else localStorage.setItem(DEV_OVERRIDE_KEY, status)
    window.dispatchEvent(new CustomEvent(BILLING_CHANGE_EVENT))
  } catch {
    // ignore — private mode etc.
  }
}

/**
 * Read the effective payment status — dev override wins if set,
 * otherwise the cached /api/billing/status response. Returns null
 * when neither is available (first paint before any fetch).
 */
export function readEffectivePaymentStatus(): string | null {
  const override = getDevPaymentOverride()
  if (override !== null) return override
  if (typeof window === "undefined") return null
  try {
    const v = localStorage.getItem(BILLING_CACHE_KEY)
    if (!v) return null
    const parsed = JSON.parse(v) as { paymentStatus?: string }
    return parsed.paymentStatus ?? null
  } catch {
    return null
  }
}

/**
 * Whether a card is on file - decoupled from being on a plan. Reads the
 * `hasPaymentMethod` field the /api/billing/status response now carries
 * (cached in BILLING_CACHE_KEY). The dev override is a payment-STATUS
 * simulator, so we map its "on a plan" states (active/past_due) to
 * having a card; it can't fake the card-on-file-but-no-plan combo, which
 * is testable for real since adding a card doesn't charge.
 */
export function readHasPaymentMethod(): boolean {
  const override = getDevPaymentOverride()
  if (override !== null) return override === "active" || override === "past_due"
  if (typeof window === "undefined") return false
  try {
    const v = localStorage.getItem(BILLING_CACHE_KEY)
    if (!v) return false
    const parsed = JSON.parse(v) as { hasPaymentMethod?: boolean }
    return parsed.hasPaymentMethod === true
  } catch {
    return false
  }
}

export type PaymentMethod = {
  type: string // "card" | "us_bank_account" | other Stripe type
  brand?: string | null
  last4: string | null
  expMonth?: number | null
  expYear?: number | null
  bankName?: string | null
}

/**
 * The payment method on file (card OR bank account) from the cached
 * billing status, or null if there's none / it hasn't loaded yet. The dev
 * payment override can't fake a specific method, so under an override this
 * returns whatever real method is cached (the UI falls back to a generic
 * "Payment method on file" label when hasPaymentMethod is true but no
 * detail is available).
 */
export function readPaymentMethod(): PaymentMethod | null {
  if (typeof window === "undefined") return null
  try {
    const v = localStorage.getItem(BILLING_CACHE_KEY)
    if (!v) return null
    const parsed = JSON.parse(v) as { paymentMethod?: PaymentMethod | null }
    return parsed.paymentMethod ?? null
  } catch {
    return null
  }
}

/**
 * Whether the payment status is actually KNOWN yet — a real cache entry
 * exists, or a dev override is set. Distinguishes "confirmed no card" from
 * "haven't checked yet", so callers can hold off on a "no payment method"
 * warning until the first /api/billing/status response lands instead of
 * flashing a false alarm on a cold login (cache empty -> reads as no-card).
 */
export function hasKnownPaymentStatus(): boolean {
  if (getDevPaymentOverride() !== null) return true
  if (typeof window === "undefined") return false
  try {
    return localStorage.getItem(BILLING_CACHE_KEY) !== null
  } catch {
    return false
  }
}

/**
 * Fetch the real billing status from the API and warm the shared cache,
 * firing BILLING_CHANGE_EVENT so every subscriber (Sidebar, Home, PlanCard)
 * re-reads immediately. Call this from any mounted view that needs the
 * status available without the user first visiting the Billing tab - e.g.
 * the Home dashboard's onboarding alerts. A dev override, if set, still
 * wins at read time; this only refreshes the real layer beneath it.
 */
export async function refreshBillingStatus(): Promise<void> {
  if (typeof window === "undefined") return
  try {
    const res = await fetch("/api/billing/status", { credentials: "include" })
    if (!res.ok) return
    const data = (await res.json()) as unknown
    localStorage.setItem(BILLING_CACHE_KEY, JSON.stringify(data))
    window.dispatchEvent(new CustomEvent(BILLING_CHANGE_EVENT))
  } catch {
    /* network / private-mode - keep last known value */
  }
}

/**
 * Whether paid backend work (tracking a channel, importing one) is locked
 * for this payment status. One helper so every lock on screen agrees.
 *
 * Mirrors the server gate exactly: `get_paid_user` in backend/app/security.py
 * admits `payment_status == "active"` and 402s everything else. Read that as
 * the contract - a UI that locks on a different predicate either blocks
 * someone the API would serve, or waves them into an error.
 *
 * `null` means billing has not been read yet, and resolves to NOT locked.
 * That polarity is a deliberate choice, and the two call sites used to
 * disagree about it: the YouTube page failed closed while Settings failed
 * open, so one cold load could paywall half a screen and not the other
 * half. Fail-open is the right default because the two mistakes are not
 * equal - briefly showing an unlocked control to someone who cannot use it
 * costs a 402 the caller already handles, while briefly showing a paywall
 * to a paying customer tells them their account is broken. The server is
 * the enforcement either way; this only decides what a half-loaded page
 * claims.
 */
export function isPaidWorkLocked(status: string | null | undefined): boolean {
  return status != null && status !== "active"
}
