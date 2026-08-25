import * as React from "react"

/**
 * Live pricing for storage + bandwidth, fetched once from the backend
 * on app mount and made available to every component via `usePrices()`.
 *
 * Source of truth lives in backend/app/billing.py (PRICE_PER_GB_PER_MONTH_USD
 * + DOWNLOAD_PRICE_PER_GB_USD). The dedicated /api/billing/prices endpoint
 * surfaces those values so the frontend can't silently drift away from
 * what users are actually billed.
 *
 * Fallback defaults match the backend constants as of the time of
 * writing; they're only used in the brief window before the fetch lands,
 * or if the request fails. Keep them in step with billing.py - they're
 * the "we're offline" safety net, not the canonical source.
 */
export type BillingPrices = {
  storagePerGbMonth: number
  downloadPerGb: number
}

const FALLBACK_PRICES: BillingPrices = {
  storagePerGbMonth: 0.02,
  downloadPerGb: 0.0,
}

const PricingContext = React.createContext<BillingPrices>(FALLBACK_PRICES)

export function PricingProvider({
  children,
}: {
  children: React.ReactNode
}) {
  const [prices, setPrices] = React.useState<BillingPrices>(FALLBACK_PRICES)

  React.useEffect(() => {
    let cancelled = false
    fetch("/api/billing/prices", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (cancelled || !data) return
        // Guard against the backend ever returning malformed JSON or
        // omitting a field - bail to fallback rather than NaN-out the
        // cost displays.
        if (
          typeof data.storagePerGbMonth === "number" &&
          typeof data.downloadPerGb === "number"
        ) {
          setPrices({
            storagePerGbMonth: data.storagePerGbMonth,
            downloadPerGb: data.downloadPerGb,
          })
        }
      })
      .catch(() => {
        // Silent - fallback prices stay in place. Real users will see
        // the right number on the next mount.
      })
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <PricingContext.Provider value={prices}>{children}</PricingContext.Provider>
  )
}

export function usePrices(): BillingPrices {
  return React.useContext(PricingContext)
}
