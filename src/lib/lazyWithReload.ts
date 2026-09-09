import { lazy, type ComponentType } from "react"

/**
 * React.lazy that recovers from a chunk which is no longer on the server.
 *
 * Asset filenames are content-hashed, so the index.html a browser loaded
 * points at chunk names from that build. A deploy used to delete the old
 * ones, and the next in-app navigation asked for a file that no longer
 * existed - "Failed to fetch dynamically imported module". Every tab that
 * happened to be open broke, silently, until someone reloaded.
 *
 * Deploys now keep old assets for 30 days, which fixes the common case.
 * This is the tail: a tab left open longer than that, or a chunk that
 * genuinely went missing. Rather than dead-ending on a blank screen, we
 * reload once - the browser fetches the current index.html and gets the
 * chunk names that exist today.
 *
 * Once, not on a loop. A reload that fails the same way would otherwise
 * refresh forever, which is worse than the error it is trying to fix: an
 * error at least says something. The marker is per-tab and cleared on the
 * next successful load, so a genuinely broken deploy shows the failure
 * instead of hiding it behind a refresh cycle.
 */
const RELOAD_MARKER = "archive336.chunk-reload"

export function lazyWithReload<T extends ComponentType<any>>(
  load: () => Promise<{ default: T }>
) {
  return lazy(async () => {
    try {
      const mod = await load()
      try {
        window.sessionStorage.removeItem(RELOAD_MARKER)
      } catch {
        // Private browsing and blocked site data both throw here. Not
        // being able to clear a marker is never a reason to fail a
        // module that loaded fine.
      }
      return mod
    } catch (err) {
      let alreadyTried = true
      try {
        alreadyTried =
          window.sessionStorage.getItem(RELOAD_MARKER) === "1"
        if (!alreadyTried) {
          window.sessionStorage.setItem(RELOAD_MARKER, "1")
        }
      } catch {
        // No usable sessionStorage means no way to know whether we have
        // already retried, and reloading blind risks a loop. Surface the
        // error instead.
        throw err
      }
      if (alreadyTried) throw err
      window.location.reload()
      // Never resolves; the reload replaces this document. Returning a
      // pending promise keeps Suspense showing its fallback rather than
      // flashing an error for the instant before navigation.
      return new Promise<{ default: T }>(() => {})
    }
  })
}
