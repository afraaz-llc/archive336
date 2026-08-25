import * as React from "react"
import { Outlet } from "react-router-dom"
import { Sidebar } from "./Sidebar"
import { DevPaymentOverlay } from "./DevPaymentOverlay"
import { ToastProvider } from "./ui/toast"
import { useScrollRestoration } from "@/lib/scrollRestoration"
import { hydrateUiPrefs, startUiPrefsAutoSync } from "@/lib/uiPrefs"

export default function AppShell() {
  useScrollRestoration()

  // Account-tied UI prefs (sidebar collapsed + layout): pull the saved
  // state on login so it follows the user across devices, and push local
  // changes back. AppShell only mounts when authed.
  React.useEffect(() => {
    void hydrateUiPrefs()
    return startUiPrefsAutoSync()
  }, [])

  return (
    <ToastProvider>
      <div className="min-h-screen flex bg-background text-foreground">
        <Sidebar />
        <main className="flex-1 min-w-0">
          <Outlet />
        </main>
      </div>
      {/* Floating dev-tool stack. The payment overlay returns null when
          its visibility flag is off, so this collapses to nothing when
          disabled. */}
      <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 items-end">
        <DevPaymentOverlay />
      </div>
    </ToastProvider>
  )
}
