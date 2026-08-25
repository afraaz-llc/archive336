import { MonitorDown } from "lucide-react"
import { Link } from "react-router-dom"
import { formatRelativeDate } from "@/lib/format"
import type { WorkerStatus } from "@/lib/workerStatus"

/**
 * Whether the backup is actually working, on the dashboard where it belongs.
 *
 * The first version of this card only knew whether the worker was ALIVE, so
 * it could only say "running" - deliberately never "backed up", because a
 * worker can check in every 30 seconds while every download fails. The
 * server now reports real health (see /worker-status), so the card can make
 * the stronger claim, and only when it is true.
 *
 * The states are ordered by what the user should do about them, not by
 * severity in the abstract. Anything that needs a human comes before
 * anything that resolves itself.
 */
export function WorkerStatusCard({ worker }: { worker: WorkerStatus }) {
  // Paused for payment outranks everything, including the app being down,
  // because it is the root cause: reopening the app would not resume a
  // single backup. Pausing silently would be worse than not pausing at all.
  if (worker.billingPaused) {
    return (
      <Row
        tone="bad"
        title="Backups are paused"
        detail="Your channels are safe, but new videos aren't being captured until your payment method works."
        action={
          <Link
            to="/settings#payment"
            className="border-2 border-border text-white font-bold px-4 py-1.5 text-sm cursor-pointer whitespace-nowrap"
          >
            Fix payment
          </Link>
        }
      />
    )
  }

  // Not running is next: the only other state where nothing happens at
  // all, and the one that silently costs the most.
  if (!worker.active) {
    return (
      <Row
        tone="bad"
        title="Backup app not running"
        detail={
          worker.lastSeenAt
            ? `Nothing has synced since ${formatRelativeDate(worker.lastSeenAt)}`
            : "Install it to start backing up"
        }
        action={
          worker.lastSeenAt ? (
            <button
              type="button"
              onClick={() => {
                window.location.href = "archive336://open"
              }}
              className="border-2 border-border text-white font-bold px-4 py-1.5 text-sm cursor-pointer whitespace-nowrap"
            >
              Open app
            </button>
          ) : (
            <Link
              to="/settings?tab=worker"
              className="border-2 border-border text-white font-bold px-4 py-1.5 text-sm cursor-pointer whitespace-nowrap inline-flex items-center gap-2"
            >
              <MonitorDown className="size-4" />
              Set up
            </Link>
          )
        }
      />
    )
  }

  // Running, but signed out of YouTube. This is the quiet one: the worker
  // keeps going and keeps succeeding, it just silently stops being able to
  // see private and members-only videos. Worth saying plainly, because
  // nothing else on screen would ever reveal it.
  if (worker.trackedChannels > 0 && !worker.youtubeAuthOk) {
    return (
      <Row
        tone="warn"
        title="Only public videos are being backed up"
        detail="The app's YouTube sign-in has lapsed, so private and members-only videos are being skipped."
        action={
          <button
            type="button"
            onClick={() => {
              window.location.href = "archive336://open"
            }}
            className="border-2 border-border text-white font-bold px-4 py-1.5 text-sm cursor-pointer whitespace-nowrap"
          >
            Authenticate
          </button>
        }
      />
    )
  }

  if (worker.failedJobs > 0) {
    return (
      <Row
        tone="warn"
        title={`${worker.failedJobs} ${worker.failedJobs === 1 ? "video" : "videos"} failed to back up`}
        detail="Everything else is up to date. Failed videos are retried automatically."
        action={
          <Link
            to="/youtube"
            className="border-2 border-border text-white font-bold px-4 py-1.5 text-sm cursor-pointer whitespace-nowrap"
          >
            Review
          </Link>
        }
      />
    )
  }

  // Working through a queue. Not "backed up" yet, and saying so would be a
  // lie of exactly the kind this card exists to avoid.
  if (worker.pendingJobs > 0) {
    return (
      <Row
        tone="ok"
        title="Backing up now"
        detail={`${worker.pendingJobs} ${worker.pendingJobs === 1 ? "video" : "videos"} left to go`}
      />
    )
  }

  // Nothing tracked yet - the app is fine, there is just nothing to do.
  if (worker.trackedChannels === 0) {
    return (
      <Row
        tone="ok"
        title="Backup app running"
        detail="Add a channel and it will start backing up automatically"
        action={
          <Link
            to="/youtube"
            className="border-2 border-border text-white font-bold px-4 py-1.5 text-sm cursor-pointer whitespace-nowrap"
          >
            Add a channel
          </Link>
        }
      />
    )
  }

  // Everything checked out: alive, signed in, nothing failed, nothing queued.
  // This is the only path allowed to claim the backup is done.
  return (
    <Row
      tone="ok"
      title="Your channels are backed up"
      detail="New videos are captured automatically"
    />
  )
}

function Row({
  tone,
  title,
  detail,
  action,
}: {
  tone: "ok" | "warn" | "bad"
  title: string
  detail: string
  action?: React.ReactNode
}) {
  const border =
    tone === "bad"
      ? "border-destructive"
      : tone === "warn"
        ? "border-amber-600"
        : "border-border"
  const dot =
    tone === "bad"
      ? "bg-muted-foreground/40"
      : tone === "warn"
        ? "bg-amber-500"
        : "bg-emerald-500"

  return (
    <div className={`border ${border} p-4 flex items-center gap-3`}>
      <span className={`inline-block size-2 rounded-full shrink-0 ${dot}`} />
      <div className="min-w-0">
        <div className="text-sm font-semibold">{title}</div>
        <div className="text-xs text-muted-foreground mt-0.5">{detail}</div>
      </div>
      {action && <div className="ml-auto shrink-0">{action}</div>}
    </div>
  )
}
