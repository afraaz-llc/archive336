import {
  SIDEBAR_PREFS_EVENT,
  readSidebarPrefs,
  writeSidebarPrefs,
  type SidebarPref,
} from "@/lib/sidebarPrefs"

/**
 * Account-tied UI preferences (sidebar collapsed state + layout).
 *
 * localStorage stays the fast local cache the Sidebar reads from; this
 * module hydrates it from the account on login (GET /api/auth/ui-prefs)
 * and debounce-pushes local changes back (PUT) so a user's sidebar state
 * follows them across devices instead of being stuck per-browser.
 */

// Sidebar collapsed lives here (not in Sidebar.tsx) so hydration can
// update it live via an event after the Sidebar has already mounted.
const COLLAPSED_KEY = "archive336.sidebar.collapsed"
export const SIDEBAR_COLLAPSED_EVENT = "archive336-sidebar-collapsed-changed"

export function readSidebarCollapsed(): boolean {
  if (typeof window === "undefined") return false
  return window.localStorage.getItem(COLLAPSED_KEY) === "true"
}

export function setSidebarCollapsed(collapsed: boolean): void {
  if (typeof window === "undefined") return
  window.localStorage.setItem(COLLAPSED_KEY, String(collapsed))
  window.dispatchEvent(new CustomEvent(SIDEBAR_COLLAPSED_EVENT))
}

type AccountUiPrefs = {
  sidebarCollapsed?: boolean
  sidebarLayout?: SidebarPref[]
}

// While true, local pref-change events don't trigger a push - used during
// hydration so we don't immediately PUT back the data we just pulled.
let suppressPush = false
let pushTimer: number | null = null

/** Pull the account's saved prefs and apply them to the local cache,
 *  firing the change events so the Sidebar updates live. Call once on
 *  login (e.g. from AppShell, which only mounts when authed). */
export async function hydrateUiPrefs(): Promise<void> {
  if (typeof window === "undefined") return
  let data: AccountUiPrefs
  try {
    const res = await fetch("/api/auth/ui-prefs", { credentials: "include" })
    if (!res.ok) return
    data = (await res.json()) as AccountUiPrefs
  } catch {
    return
  }
  suppressPush = true
  try {
    if (typeof data.sidebarCollapsed === "boolean") {
      setSidebarCollapsed(data.sidebarCollapsed)
    }
    if (Array.isArray(data.sidebarLayout)) {
      writeSidebarPrefs(data.sidebarLayout)
    }
  } finally {
    // Let the just-fired events settle before re-enabling push.
    window.setTimeout(() => {
      suppressPush = false
    }, 0)
  }
}

function pushNow(): void {
  if (typeof window === "undefined") return
  const body: AccountUiPrefs = {
    sidebarCollapsed: readSidebarCollapsed(),
    sidebarLayout: readSidebarPrefs(),
  }
  void fetch("/api/auth/ui-prefs", {
    method: "PUT",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).catch(() => {
    /* offline / transient - localStorage already holds the value */
  })
}

function schedulePush(): void {
  if (suppressPush) return
  if (pushTimer !== null) window.clearTimeout(pushTimer)
  pushTimer = window.setTimeout(pushNow, 600)
}

/** Subscribe to local sidebar-pref changes and debounce-push them to the
 *  account. Returns an unsubscribe fn. Call once while authed. */
export function startUiPrefsAutoSync(): () => void {
  if (typeof window === "undefined") return () => {}
  window.addEventListener(SIDEBAR_PREFS_EVENT, schedulePush)
  window.addEventListener(SIDEBAR_COLLAPSED_EVENT, schedulePush)
  return () => {
    window.removeEventListener(SIDEBAR_PREFS_EVENT, schedulePush)
    window.removeEventListener(SIDEBAR_COLLAPSED_EVENT, schedulePush)
    if (pushTimer !== null) window.clearTimeout(pushTimer)
  }
}
