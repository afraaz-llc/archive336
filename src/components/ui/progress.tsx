import * as React from "react"
import { cn } from "@/lib/utils"

type Props = React.HTMLAttributes<HTMLDivElement> & {
  value?: number
  barClassName?: string
}

export function Progress({ value = 0, className, barClassName, ...props }: Props) {
  const pct = Math.max(0, Math.min(1, value)) * 100
  return (
    <div
      className={cn("h-1.5 w-full overflow-hidden rounded-full bg-muted", className)}
      role="progressbar"
      aria-valuenow={Math.round(pct)}
      aria-valuemin={0}
      aria-valuemax={100}
      {...props}
    >
      <div
        className={cn("h-full bg-primary", barClassName)}
        style={{ width: `${pct}%` }}
      />
    </div>
  )
}
