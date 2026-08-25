import * as React from "react"
import { Link } from "react-router-dom"
import { Lock } from "lucide-react"

/**
 * Wrap any chunk of UI to render it as locked-behind-payment: the
 * children are blurred + dimmed and pointer events disabled, with a
 * centered CTA pointing to /settings#payment.
 *
 * Visual test for the "no payment method" theme — if the team likes
 * the treatment we'll apply it to the YouTube page Add Channel form,
 * the channel-detail sync buttons, and the Home empty state too.
 */
export function Paywalled({
  children,
  message = "Add a payment method to use this",
  iconOnly = false,
  className = "",
}: {
  children: React.ReactNode
  message?: string
  /** When true, only the lock icon is rendered centered. Use this on
   *  pages where another nearby element already provides the CTA
   *  (e.g. Settings, where the Plan card sits right above with its
   *  own Add-payment-method button). */
  iconOnly?: boolean
  className?: string
}) {
  return (
    <div className={"relative " + className}>
      {/* Blurred + dimmed content. aria-hidden so screen readers skip it
          since it's not actionable in this state. */}
      <div
        aria-hidden
        className="pointer-events-none select-none blur-[3px] opacity-40"
      >
        {children}
      </div>

      {/* Overlay */}
      <div className="absolute inset-0 flex items-center justify-center">
        {iconOnly ? (
          <Lock className="size-6 text-muted-foreground" />
        ) : (
          <div className="flex flex-col items-center gap-3 text-center px-4">
            <Lock className="size-5 text-muted-foreground" />
            <p className="text-sm font-semibold">{message}</p>
            <Link
              to="/settings#payment"
              className="inline-flex items-center text-xs uppercase tracking-wider font-bold text-primary border border-primary/40 px-3 py-1.5 hover:bg-primary/10"
            >
              Add payment method
            </Link>
          </div>
        )}
      </div>
    </div>
  )
}
