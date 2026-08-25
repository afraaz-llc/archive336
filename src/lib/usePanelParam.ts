import * as React from "react"
import { useSearchParams } from "react-router-dom"

/**
 * Derive a sheet/sidepanel's open/close state from a URL search param
 * so panels stay open across page refresh and are share-link friendly.
 *
 * Usage:
 *   const [syncOpen, setSyncOpen] = usePanelParam("sync")
 *   const [downloadOpen, setDownloadOpen] = usePanelParam("download")
 *
 * All panels on a given page share the same `panel` param key. Opening
 * one implicitly closes any other - last write wins. That matches the
 * UX where Sheet components stack with overlays anyway (only one is
 * usable at a time even if technically rendered).
 *
 * The page-specific `?video=<id>` param continues to work in parallel
 * because it uses a different key.
 */

const PARAM_KEY = "panel"

export function usePanelParam(
  name: string
): [boolean, (open: boolean) => void] {
  const [searchParams, setSearchParams] = useSearchParams()
  const isOpen = searchParams.get(PARAM_KEY) === name

  const setOpen = React.useCallback(
    (open: boolean) => {
      setSearchParams(
        (curr) => {
          const next = new URLSearchParams(curr)
          if (open) {
            next.set(PARAM_KEY, name)
          } else if (next.get(PARAM_KEY) === name) {
            // Only delete the param if WE were the one that set it -
            // avoids clobbering a different panel that swapped in
            // between (rare but possible if the user clicks fast).
            next.delete(PARAM_KEY)
          }
          return next
        },
        { replace: false }
      )
    },
    [name, setSearchParams]
  )

  return [isOpen, setOpen]
}
