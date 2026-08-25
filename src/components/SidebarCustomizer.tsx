import * as React from "react"
import { ChevronDown, ChevronUp } from "lucide-react"
import { Switch } from "@/components/ui/switch"
import {
  SIDEBAR_PREFS_EVENT,
  labelForSidebarItem,
  readSidebarPrefs,
  writeSidebarPrefs,
  type SidebarItemId,
  type SidebarPref,
} from "@/lib/sidebarPrefs"

/**
 * General-settings control for the left sidebar: toggle each nav item
 * on/off and reorder it with the up/down arrows. Writes go straight to
 * the shared prefs store, which broadcasts to the live Sidebar so the
 * change shows immediately.
 */
export function SidebarCustomizer() {
  const [prefs, setPrefs] = React.useState<SidebarPref[]>(() =>
    readSidebarPrefs()
  )

  React.useEffect(() => {
    const sync = () => setPrefs(readSidebarPrefs())
    window.addEventListener(SIDEBAR_PREFS_EVENT, sync)
    return () => window.removeEventListener(SIDEBAR_PREFS_EVENT, sync)
  }, [])

  const commit = (next: SidebarPref[]) => {
    setPrefs(next)
    writeSidebarPrefs(next)
  }

  const toggle = (id: SidebarItemId) =>
    commit(prefs.map((p) => (p.id === id ? { ...p, enabled: !p.enabled } : p)))

  const move = (index: number, dir: -1 | 1) => {
    const j = index + dir
    if (j < 0 || j >= prefs.length) return
    const next = prefs.slice()
    const tmp = next[index]
    next[index] = next[j]
    next[j] = tmp
    commit(next)
  }

  return (
    <section>
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">
        Sidebar
      </div>
      <div className="space-y-2">
        {prefs.map((p, i) => {
          const label = labelForSidebarItem(p.id)
          return (
            <div
              key={p.id}
              className="flex items-center gap-3 border border-border p-3"
            >
              <div className="flex flex-col -my-1.5">
                <button
                  type="button"
                  onClick={() => move(i, -1)}
                  disabled={i === 0}
                  aria-label={`Move ${label} up`}
                  className="cursor-pointer text-muted-foreground disabled:cursor-default disabled:opacity-25"
                >
                  <ChevronUp className="size-4" />
                </button>
                <button
                  type="button"
                  onClick={() => move(i, 1)}
                  disabled={i === prefs.length - 1}
                  aria-label={`Move ${label} down`}
                  className="cursor-pointer text-muted-foreground disabled:cursor-default disabled:opacity-25"
                >
                  <ChevronDown className="size-4" />
                </button>
              </div>
              <div className="min-w-0 flex-1 text-sm font-medium">{label}</div>
              <Switch
                checked={p.enabled}
                onCheckedChange={() => toggle(p.id)}
                aria-label={`Show ${label} in sidebar`}
              />
            </div>
          )
        })}
      </div>
    </section>
  )
}
