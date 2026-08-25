import * as React from "react"

/**
 * Sets `document.title` for the lifetime of the component. Restores
 * the previous title on unmount so leaving a page doesn't strand a
 * stale title until the next page sets its own.
 *
 * Usage: `useDocumentTitle("YouTube")` produces a tab title of
 * "YouTube · ARCHIVE336". The base name comes from the
 * <title> in index.html and is auto-appended unless you opt out
 * with `appendBase: false` (rare — only useful for the home page
 * where the base is the whole title).
 */
const BASE_TITLE = "ARCHIVE336"

export function useDocumentTitle(
  title: string | null | undefined,
  opts: { appendBase?: boolean } = {},
): void {
  const appendBase = opts.appendBase ?? true
  React.useEffect(() => {
    const prev = document.title
    if (title) {
      document.title = appendBase ? `${title} · ${BASE_TITLE}` : title
    } else {
      document.title = BASE_TITLE
    }
    return () => {
      document.title = prev
    }
  }, [title, appendBase])
}
