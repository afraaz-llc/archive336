import { cn } from "@/lib/utils"

/**
 * Tiny colored-dot + uppercase label badge - mirrors the website's
 * AccountBox status indicator. Use sparingly: the headline state of
 * a card (connected, running, down) is what this is for.
 */

export type Status = "active" | "warning" | "down" | "idle"

const dotColor: Record<Status, string> = {
  active: "bg-green-500",
  warning: "bg-yellow-500",
  down: "bg-red-500",
  idle: "bg-muted-foreground",
}

const labelColor: Record<Status, string> = {
  active: "text-green-500",
  warning: "text-yellow-500",
  down: "text-red-500",
  idle: "text-muted-foreground",
}

export function StatusPill({
  status,
  label,
  detail,
  className,
}: {
  status: Status
  label?: string
  detail?: string
  className?: string
}) {
  const text = label ?? defaultLabel(status)
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 text-[10px] uppercase tracking-wider font-bold",
        labelColor[status],
        className,
      )}
      title={detail}
    >
      <span
        aria-hidden
        className={cn("inline-block w-1.5 h-1.5 rounded-full", dotColor[status])}
      />
      {text}
    </span>
  )
}

function defaultLabel(s: Status): string {
  switch (s) {
    case "active":
      return "Active"
    case "warning":
      return "Warning"
    case "down":
      return "Down"
    case "idle":
      return "Idle"
  }
}
