import * as React from "react"

/**
 * Polls /api/youtube/worker-status to tell the UI whether the user's
 * desktop worker app is currently up.
 *
 * Polling cadence:
 *   - 3s when active polling is requested (e.g. there's a syncing
 *     video on screen, or the SyncPanel is open)
 *   - Skips polling entirely when shouldPoll=false to keep the
 *     network quiet on pages that don't care
 *
 * Server treats a worker as 'active' if any worker-UA session has
 * bumped last_seen_at within ~30s, so a 3s poll cadence means the
 * UI catches a freshly-started worker within one tick.
 */

export type WorkerStatus = {
  active: boolean
  lastSeenAt: string | null
  /** Paid work is paused (failed / missing card). Nothing new is being
   *  backed up, so no green state may claim otherwise. */
  billingPaused: boolean
  /** Worker's YouTube sign-in still usable. Without it only public
   *  videos can be captured, which is a silent downgrade, not an error. */
  youtubeAuthOk: boolean
  youtubeReportedAt: string | null
  /** Jobs that failed in the last 24h. */
  failedJobs: number
  /** Queued or in-flight jobs. */
  pendingJobs: number
  trackedChannels: number
}

const DEFAULT: WorkerStatus = {
  active: false,
  lastSeenAt: null,
  billingPaused: false,
  youtubeAuthOk: false,
  youtubeReportedAt: null,
  failedJobs: 0,
  pendingJobs: 0,
  trackedChannels: 0,
}

export function useWorkerStatus(shouldPoll: boolean): WorkerStatus {
  const [status, setStatus] = React.useState<WorkerStatus>(DEFAULT)

  React.useEffect(() => {
    if (!shouldPoll) return
    let cancelled = false

    const fetchOnce = async () => {
      try {
        const res = await fetch("/api/youtube/worker-status", {
          credentials: "include",
        })
        if (!res.ok || cancelled) return
        const data = (await res.json()) as WorkerStatus
        if (!cancelled) setStatus(data)
      } catch {
        // Transient - keep last-known value rather than flipping to
        // "inactive" on a single network hiccup.
      }
    }

    void fetchOnce()
    const id = window.setInterval(fetchOnce, 3000)
    return () => {
      cancelled = true
      window.clearInterval(id)
    }
  }, [shouldPoll])

  return status
}
