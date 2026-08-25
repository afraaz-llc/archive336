import * as React from "react"
import { Link } from "react-router-dom"
import { ArrowLeft } from "lucide-react"

/**
 * Shared chrome for the public legal pages (Terms, Privacy).
 *
 * Sits outside RequireAuth so anyone — including someone reading the
 * pricing page before they sign up — can read the docs. Intentionally
 * minimal: just a centered column, a back-to-home link, and typography
 * tuned for long-form reading.
 */
export function LegalLayout({
  title,
  lastUpdated,
  children,
}: {
  title: string
  lastUpdated: string
  children: React.ReactNode
}) {
  return (
    <div className="min-h-screen bg-background text-foreground legal-selectable">
      <div className="max-w-3xl mx-auto px-6 py-10">
        <Link
          to="/"
          className="inline-flex items-center gap-2 text-xs uppercase tracking-wider text-muted-foreground font-semibold hover:text-foreground"
        >
          <ArrowLeft className="size-4" />
          Back
        </Link>

        <h1 className="text-3xl font-extrabold tracking-tight mt-6">{title}</h1>
        <p className="text-xs text-muted-foreground mt-1">
          Last updated {lastUpdated}
        </p>

        <div className="legal-prose mt-10 space-y-6 text-sm leading-relaxed">
          {children}
        </div>
      </div>
    </div>
  )
}

/* Section heading — h2-style with a small uppercase eyebrow above. */
export function Section({
  number,
  title,
  children,
}: {
  number?: string
  title: string
  children: React.ReactNode
}) {
  return (
    <section>
      {number && (
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold">
          Section {number}
        </div>
      )}
      <h2 className="text-lg font-bold mt-1">{title}</h2>
      <div className="mt-3 space-y-3">{children}</div>
    </section>
  )
}
