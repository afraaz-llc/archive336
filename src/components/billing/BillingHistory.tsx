import * as React from "react"
import { Download } from "lucide-react"

type Invoice = {
  id: string
  number: string | null
  created: number | null
  total: number | null
  amountPaid: number | null
  currency: string | null
  status: string | null
  hostedUrl: string | null
  pdfUrl: string | null
}

function formatInvoiceDate(unix: number | null): string {
  if (!unix) return "—"
  return new Date(unix * 1000).toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
  })
}

function formatAmount(inv: Invoice): string {
  const cents = inv.total ?? inv.amountPaid ?? 0
  const amount = (cents / 100).toFixed(2)
  const cur = (inv.currency ?? "usd").toUpperCase()
  return cur === "USD" ? `$${amount}` : `${amount} ${cur}`
}

function statusLabel(status: string | null): string {
  if (!status) return ""
  return status.charAt(0).toUpperCase() + status.slice(1)
}

/**
 * Settings → Billing → "Billing history": the user's past Stripe invoices,
 * newest first, each linking to its PDF/hosted receipt. Pulled live from
 * /api/billing/invoices; shows an empty state until they've been billed.
 * Never paywalled — people always need access to their own receipts.
 */
export function BillingHistory() {
  const [invoices, setInvoices] = React.useState<Invoice[] | null>(null)

  React.useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const res = await fetch("/api/billing/invoices", {
          credentials: "include",
        })
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const data = (await res.json()) as { invoices?: Invoice[] }
        if (!cancelled) setInvoices(data.invoices ?? [])
      } catch {
        if (!cancelled) setInvoices([])
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  if (invoices === null) {
    return (
      <div className="border border-border p-4 text-sm text-muted-foreground">
        Loading…
      </div>
    )
  }

  if (invoices.length === 0) {
    return (
      <div className="border border-border p-4 text-sm text-muted-foreground">
        No invoices yet. Charges appear here once you've been billed.
      </div>
    )
  }

  return (
    <div className="border border-border divide-y divide-border">
      {invoices.map((inv) => {
        const link = inv.pdfUrl ?? inv.hostedUrl
        return (
          <div key={inv.id} className="flex items-center gap-4 p-4">
            <div className="min-w-0 flex-1">
              <div className="text-sm font-semibold">
                {formatInvoiceDate(inv.created)}
              </div>
              <div className="text-xs text-muted-foreground mt-0.5 font-mono">
                {formatAmount(inv)}
                {inv.status ? ` · ${statusLabel(inv.status)}` : ""}
              </div>
            </div>
            {link && (
              <a
                href={link}
                target="_blank"
                rel="noopener noreferrer"
                className="shrink-0 inline-flex items-center gap-1.5 text-xs uppercase tracking-wider font-semibold text-muted-foreground hover:text-foreground"
              >
                <Download className="size-3.5" />
                Receipt
              </a>
            )}
          </div>
        )
      })}
    </div>
  )
}
