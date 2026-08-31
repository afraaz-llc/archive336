/**
 * User-controlled left-sidebar layout: which nav items appear and in
 * what order. Persisted in localStorage and broadcast via a custom
 * event so the Sidebar re-renders the instant the General settings
 * customizer changes it.
 *
 * Scope: only the customizable content items live here. Admin/Dev are
 * is_admin-gated dev tools (always shown for admins) and Settings is a
 * fixed footer entry - none of those are user-reorderable, so they're
 * deliberately excluded.
 *
 * Storage holds an ordered [{id, enabled}] list. On read we reconcile
 * it against the canonical item registry: unknown ids are dropped, and
 * any newly-added item the stored prefs predate is appended (enabled),
 * so shipping a new nav item never leaves it hidden.
 */

export type SidebarItemId = "home" | "youtube"

export const SIDEBAR_ITEMS: ReadonlyArray<{ id: SidebarItemId; label: string }> = [
  { id: "home", label: "Home" },
  { id: "youtube", label: "YouTube" },
  // Support used to be here. It is a Settings tab now - the reconcile
  // -on-read below drops ids it does not recognise, so anyone who
  // already had it saved loses the entry without needing a migration.
]

export type SidebarPref = { id: SidebarItemId; enabled: boolean }

const KEY = "archive336_sidebar_prefs_v1"
export const SIDEBAR_PREFS_EVENT = "archive336-sidebar-prefs-changed"

function defaults(): SidebarPref[] {
  return SIDEBAR_ITEMS.map((i) => ({ id: i.id, enabled: true }))
}

export function readSidebarPrefs(): SidebarPref[] {
  if (typeof window === "undefined") return defaults()
  let stored: SidebarPref[] = []
  try {
    const raw = window.localStorage.getItem(KEY)
    if (raw) stored = JSON.parse(raw) as SidebarPref[]
  } catch {
    return defaults()
  }
  const known = new Set<SidebarItemId>(SIDEBAR_ITEMS.map((i) => i.id))
  const seen = new Set<SidebarItemId>()
  const out: SidebarPref[] = []
  for (const p of stored) {
    if (p && known.has(p.id) && !seen.has(p.id)) {
      out.push({ id: p.id, enabled: p.enabled !== false })
      seen.add(p.id)
    }
  }
  for (const i of SIDEBAR_ITEMS) {
    if (!seen.has(i.id)) out.push({ id: i.id, enabled: true })
  }
  return out
}

export function writeSidebarPrefs(prefs: SidebarPref[]): void {
  if (typeof window === "undefined") return
  try {
    window.localStorage.setItem(KEY, JSON.stringify(prefs))
    window.dispatchEvent(new CustomEvent(SIDEBAR_PREFS_EVENT))
  } catch {
    /* private mode / quota - ignore */
  }
}

export function labelForSidebarItem(id: SidebarItemId): string {
  return SIDEBAR_ITEMS.find((i) => i.id === id)?.label ?? id
}
