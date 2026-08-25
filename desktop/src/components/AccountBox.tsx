import * as React from "react"
import { cn } from "@/lib/utils"
import { StatusPill, type Status } from "./StatusPill"

/**
 * Container for a single "thing" the user manages - currently the
 * YouTube account, but the same shape will fit future connections
 * (Google Drive, Dropbox, etc.) without redesigning each one.
 *
 * Mirrors the website's CloudflareAccountBox / StripeAccountBox
 * anatomy: header (name + status + action buttons) → optional
 * description → arbitrary body content.
 */
export function AccountBox({
  name,
  status,
  statusLabel,
  statusDetail,
  description,
  actions,
  children,
  className,
}: {
  name: string
  /** Omit to render no pill at all. Absence is the right answer when a
   *  state needs no comment - see the channel cards, where "not
   *  authenticated" is a deliberate choice rather than a status worth
   *  reporting. */
  status?: Status
  /** Override the StatusPill's default label (e.g. "Connected · 47 cookies"). */
  statusLabel?: string
  /** Tooltip on the status pill, useful for the longer error reason. */
  statusDetail?: string
  /** Short paragraph under the header, like the website's box descriptions. */
  description?: React.ReactNode
  /** Buttons aligned right of the header (Reconnect, Re-check, etc.). */
  actions?: React.ReactNode
  /** Body content - forms, detail tables, anything custom. */
  children?: React.ReactNode
  className?: string
}) {
  return (
    // space-y rather than a margin on the header, because only space-y
    // gets this right. `children` is a truthy fragment even when every
    // conditional inside it renders nothing, so a card with just a header
    // still reserved a gap below and sat visibly off-centre in its own
    // padding. space-y adds margin BETWEEN rendered siblings, so a body
    // that collapses to nothing costs nothing.
    <section
      className={cn("border border-border p-4 space-y-3", className)}
    >
      {/* Header row: name + status on the left, actions on the right. */}
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-2.5 min-w-0">
          <h2 className="text-sm font-semibold leading-none">{name}</h2>
          {status && (
            <StatusPill
              status={status}
              label={statusLabel}
              detail={statusDetail}
            />
          )}
        </div>
        {actions && (
          <div className="flex items-center gap-2 shrink-0">{actions}</div>
        )}
      </div>

      {description && (
        <p className="text-xs text-muted-foreground leading-relaxed">
          {description}
        </p>
      )}

      {children}
    </section>
  )
}
