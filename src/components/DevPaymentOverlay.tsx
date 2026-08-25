import * as React from "react"
import { FlaskConical } from "lucide-react"
import { useAuth } from "@/auth/AuthContext"
import {
  BILLING_CHANGE_EVENT,
  DEV_OVERLAY_CHANGE_EVENT,
  getDevOverlayVisible,
  getDevPaymentOverride,
  setDevPaymentOverride,
} from "@/lib/paymentStatus"

/**
 * Floating bottom-right pill that lets an admin flip the payment-status
 * override on any page. Renders only when:
 *   - The current user is admin
 *   - The overlay-visible flag is set (toggled from /dev)
 *
 * Reuses the same mechanism the Dev page used inline before - this
 * component just pulls it out of /dev so the iteration loop can stay
 * on whatever page is being designed.
 */

export function DevPaymentOverlay() {
  const { state } = useAuth()
  const [visible, setVisible] = React.useState<boolean>(() =>
    getDevOverlayVisible()
  )
  const [override, setOverrideState] = React.useState<string | null>(() =>
    getDevPaymentOverride()
  )

  React.useEffect(() => {
    const syncVisible = () => setVisible(getDevOverlayVisible())
    const syncOverride = () => setOverrideState(getDevPaymentOverride())
    window.addEventListener(DEV_OVERLAY_CHANGE_EVENT, syncVisible)
    window.addEventListener(BILLING_CHANGE_EVENT, syncOverride)
    // 'storage' covers updates from other tabs.
    window.addEventListener("storage", syncVisible)
    window.addEventListener("storage", syncOverride)
    return () => {
      window.removeEventListener(DEV_OVERLAY_CHANGE_EVENT, syncVisible)
      window.removeEventListener(BILLING_CHANGE_EVENT, syncOverride)
      window.removeEventListener("storage", syncVisible)
      window.removeEventListener("storage", syncOverride)
    }
  }, [])

  if (state.status !== "authed") return null
  if (!state.user.is_admin) return null
  if (!visible) return null

  const set = (next: string | null) => {
    setDevPaymentOverride(next)
    setOverrideState(next)
  }

  return (
    <div className="inline-flex items-center gap-1.5 border border-border bg-background p-1.5 text-xs shadow-lg">
      <div className="flex items-center gap-1.5 pl-1.5 pr-1 text-muted-foreground">
        <FlaskConical className="size-3.5" />
        <span className="uppercase tracking-wider font-bold text-[10px]">
          dev
        </span>
      </div>
      <Pill active={override === null} onClick={() => set(null)}>
        Real
      </Pill>
      <Pill active={override === "active"} onClick={() => set("active")}>
        Has card
      </Pill>
      <Pill active={override === "none"} onClick={() => set("none")}>
        No card
      </Pill>
    </div>
  )
}

function Pill({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={
        "px-2 py-1 cursor-pointer border " +
        (active
          ? "bg-foreground text-background border-foreground font-bold"
          : "border-border text-muted-foreground hover:text-foreground")
      }
    >
      {children}
    </button>
  )
}
