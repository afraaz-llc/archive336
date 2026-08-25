import { useEffect } from "react"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

/**
 * Status tab's "is this thing actually set up?" section.
 *
 * Shows a row per gate that has to pass before the worker can sync.
 * The whole component returns null once everything is granted - keeps
 * the Status tab clean in steady state.
 */

export type SetupReadiness = "loading" | "incomplete" | "ready"

type RowState = "pending" | "verifying" | "granted" | "denied"

type Row = {
  id: string
  label: string
  state: RowState
  detail?: string
  action?: { label: string; onClick: () => void; busy?: boolean }
  required: boolean
}

type Props = {
  hasCredentials: boolean
  ytdlpInstalled: boolean
  onReadinessChange?: (ready: SetupReadiness) => void
  /**
   * How many channels the website says this account tracks.
   *
   * Zero is not a broken setup, it is a brand-new account, and the two
   * need opposite instructions. Telling someone with no channels to
   * "sign in to your YouTube account" sends them through a sign-in that
   * lands nowhere - the worker only authenticates channels the website
   * already tracks. `null` while the list is still loading.
   */
  trackedChannelCount: number | null
  /** Open the website's YouTube page so a channel can be added. */
  onAddChannel: () => void
}

export function SetupChecklist({
  hasCredentials,
  ytdlpInstalled,
  onReadinessChange,
  trackedChannelCount,
  onAddChannel,
}: Props) {
  // null (still loading) must not read as "no channels" - that would flash
  // "Add a channel" at someone who has plenty.
  const noChannels = trackedChannelCount === 0

  const rows: Row[] = [
    {
      id: "account",
      label: "Account credentials saved",
      state: hasCredentials ? "granted" : "pending",
      detail: hasCredentials
        ? undefined
        : "Add your username and password in Settings.",
      required: true,
    },
    {
      id: "ytdlp",
      label: "yt-dlp installed",
      state: ytdlpInstalled ? "granted" : "denied",
      detail: ytdlpInstalled
        ? undefined
        : "Install yt-dlp first (e.g. brew install yt-dlp), then restart this app.",
      required: true,
    },
  ]

  // A channel to back up is a real gate - with none there is nothing to
  // sync. Authentication is NOT: it upgrades a channel from public-only
  // to including private videos, and it belongs on the Connections tab
  // where it is offered per channel. It used to sit here too, which read
  // as a setup step the app was waiting on when nothing was waiting.
  if (noChannels) {
    rows.push({
      id: "channel",
      label: "A channel to back up",
      state: "pending",
      action: { label: "Add a channel", onClick: onAddChannel },
      required: true,
    })
  }

  const requiredRows = rows.filter((r) => r.required)
  const allGreen = requiredRows.every((r) => r.state === "granted")

  // Surface readiness to the parent (so the Sync section can disable
  // its Start button while setup is incomplete).
  useEffect(() => {
    if (!onReadinessChange) return
    if (trackedChannelCount === null) {
      onReadinessChange("loading")
    } else if (allGreen) {
      onReadinessChange("ready")
    } else {
      onReadinessChange("incomplete")
    }
  }, [allGreen, trackedChannelCount, onReadinessChange])

  // Nothing left to set up.
  if (rows.every((r) => r.state === "granted")) return null

  return (
    <section className="border border-border p-4">
      <h2 className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">
        Setup
      </h2>
      <ul className="flex flex-col gap-3">
        {rows.map((r) => (
          <li key={r.id} className="flex items-start gap-2.5">
            <RowIcon state={r.state} />
            <div className="flex-1 min-w-0">
              <div className="text-sm font-semibold">{r.label}</div>
              {r.detail && (
                <div
                  className={cn(
                    "text-xs mt-0.5 leading-relaxed",
                    r.state === "denied"
                      ? "text-destructive"
                      : "text-muted-foreground",
                  )}
                >
                  {r.detail}
                </div>
              )}
            </div>
            {r.action && (
              <Button
                variant="outline"
                size="sm"
                onClick={r.action.onClick}
                disabled={r.action.busy}
                className="shrink-0"
              >
                {r.action.busy ? "Working…" : r.action.label}
              </Button>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}

function RowIcon({ state }: { state: RowState }) {
  const colorClasses =
    state === "granted"
      ? "border-green-500 text-green-500"
      : state === "denied"
        ? "border-destructive text-destructive"
        : state === "verifying"
          ? "border-foreground text-foreground"
          : "border-muted-foreground text-muted-foreground"
  return (
    <span
      aria-hidden
      className={cn(
        "inline-flex items-center justify-center size-5 shrink-0 border text-xs font-bold mt-0.5",
        colorClasses,
      )}
    >
      {iconFor(state)}
    </span>
  )
}

function iconFor(state: RowState): string {
  switch (state) {
    case "granted":
      return "✓"
    case "denied":
      return "✕"
    case "verifying":
      return "…"
    case "pending":
    default:
      return "!"
  }
}
