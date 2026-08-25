import * as React from "react"
import { Link } from "react-router-dom"

/**
 * Account-setup warning row: a destructive-bordered alert whose ENTIRE
 * surface is the clickable button — an icon plus an action-phrased title
 * ("Verify your email address", "Add a payment method") that routes into
 * the relevant Settings section. Used by the Home dashboard's onboarding
 * alerts and the General settings tab so the two never drift.
 */
export function SetupAlert({
  icon,
  title,
  to,
}: {
  icon: React.ReactNode
  title: string
  to: string
}) {
  return (
    <Link
      to={to}
      className="flex items-center gap-4 border border-destructive p-4 text-sm font-semibold text-destructive cursor-pointer hover:bg-destructive hover:text-destructive-foreground"
    >
      <span className="shrink-0">{icon}</span>
      <span className="min-w-0 flex-1">{title}</span>
    </Link>
  )
}
