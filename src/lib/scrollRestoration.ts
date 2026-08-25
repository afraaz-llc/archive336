import * as React from "react"
import { useLocation } from "react-router-dom"

/**
 * Persist scroll position per route across refresh and back/forward
 * navigation. SessionStorage (not localStorage) so positions reset on
 * tab close - it's "where I was a moment ago," not "where I was last
 * week."
 *
 * Key is pathname only. Search params used for panel-overlay state
 * (?video=<id>, ?panel=settings, etc.) don't represent navigation to
 * a different scroll context - they're sheets layered on top of the
 * same underlying page. If we keyed by search too, opening a video
 * panel would reset scroll to top (no saved position for the new
 * URL), then closing the panel would restore the prior position,
 * creating a visible jump.
 *
 * If a URL has a hash anchor we skip restore so per-page hash
 * effects (e.g. Settings' #payment scroll-to-center) stay in charge.
 */

const KEY = "archive336_scroll_positions_v1"
const SAVE_DEBOUNCE_MS = 150
// On refresh, content with async fetches (e.g. channels list) hasn't
// painted yet on the first frame, so the saved scroll Y may exceed
// document.body.scrollHeight. Retry across a handful of frames before
// giving up. ~600ms is enough for the slowest fetch-on-mount we have
// without making the page feel laggy if the position is genuinely
// unreachable.
const RESTORE_MAX_FRAMES = 40

type ScrollMap = Record<string, number>

function readMap(): ScrollMap {
  if (typeof window === "undefined") return {}
  try {
    return JSON.parse(sessionStorage.getItem(KEY) || "{}") as ScrollMap
  } catch {
    return {}
  }
}

function writeMap(map: ScrollMap): void {
  if (typeof window === "undefined") return
  try {
    sessionStorage.setItem(KEY, JSON.stringify(map))
  } catch {
    /* private mode / quota - ignore */
  }
}

export function useScrollRestoration(): void {
  const location = useLocation()
  // Key by pathname only - see file header for why.
  const path = location.pathname
  const hasHash = location.hash.length > 0

  // Disable the browser's own attempt at restoring scroll on refresh -
  // for SPAs it usually fires before content paints and gets clamped to
  // 0. We handle it ourselves.
  React.useEffect(() => {
    if ("scrollRestoration" in window.history) {
      window.history.scrollRestoration = "manual"
    }
  }, [])

  // Restore on path change (which covers both back/forward navigation
  // and the initial mount on refresh).
  React.useEffect(() => {
    if (hasHash) return
    const map = readMap()
    const target = map[path]
    if (typeof target !== "number") {
      // No saved position for this route - default to top instead of
      // wherever the user left off on the previous route.
      window.scrollTo({ top: 0, behavior: "auto" })
      return
    }
    let frames = 0
    let raf = 0
    const tryRestore = () => {
      const maxY =
        document.documentElement.scrollHeight - window.innerHeight
      // If the doc is now tall enough, set position and stop.
      if (maxY >= target) {
        window.scrollTo({ top: target, behavior: "auto" })
        return
      }
      // Otherwise the page still hasn't grown enough - scroll as far
      // as we can and try again next frame.
      window.scrollTo({ top: maxY > 0 ? maxY : 0, behavior: "auto" })
      frames += 1
      if (frames < RESTORE_MAX_FRAMES) {
        raf = window.requestAnimationFrame(tryRestore)
      }
    }
    raf = window.requestAnimationFrame(tryRestore)
    return () => {
      window.cancelAnimationFrame(raf)
    }
  }, [path, hasHash])

  // Save on scroll (debounced), on beforeunload (refresh / close), and
  // on unmount (intra-app navigation).
  React.useEffect(() => {
    let timeout: number | null = null
    const save = () => {
      const map = readMap()
      map[path] = window.scrollY
      writeMap(map)
    }
    const onScroll = () => {
      if (timeout !== null) window.clearTimeout(timeout)
      timeout = window.setTimeout(save, SAVE_DEBOUNCE_MS)
    }
    window.addEventListener("scroll", onScroll, { passive: true })
    window.addEventListener("beforeunload", save)
    return () => {
      if (timeout !== null) window.clearTimeout(timeout)
      window.removeEventListener("scroll", onScroll)
      window.removeEventListener("beforeunload", save)
      save()
    }
  }, [path])
}
