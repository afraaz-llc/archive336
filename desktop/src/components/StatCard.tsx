import * as React from "react"
import { cn } from "@/lib/utils"

/**
 * Single metric tile: small uppercase label on top, big mono value
 * below. Same anatomy the website uses 50+ times in the Admin tab.
 *
 * Optional `note` line under the value for sub-detail (units,
 * deltas, "as of …"). Optional `icon` shows next to the label.
 */
export function StatCard({
  label,
  value,
  icon,
  note,
  className,
}: {
  label: string
  value: React.ReactNode
  icon?: React.ReactNode
  note?: string
  className?: string
}) {
  return (
    <div className={cn("border border-border p-4", className)}>
      <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-muted-foreground font-semibold">
        {icon}
        <span>{label}</span>
      </div>
      <div className="mt-1.5 text-lg font-bold font-mono tabular-nums">
        {value}
      </div>
      {note && (
        <div className="mt-1 text-[11px] text-muted-foreground">{note}</div>
      )}
    </div>
  )
}

/**
 * Convenience grid wrapper for a row of StatCards. Defaults to 2
 * columns on mobile, scales up. Override by passing your own grid
 * classes via `className`.
 */
export function StatGrid({
  children,
  className,
}: {
  children: React.ReactNode
  className?: string
}) {
  return (
    <div
      className={cn(
        "grid grid-cols-2 sm:grid-cols-3 gap-3",
        className,
      )}
    >
      {children}
    </div>
  )
}
