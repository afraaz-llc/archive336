export function formatDuration(sec: number): string {
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  const s = Math.floor(sec % 60)
  if (h > 0) return `${h}:${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`
  return `${m}:${s.toString().padStart(2, "0")}`
}

export function formatCount(n: number): string {
  if (n >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(1).replace(/\.0$/, "")}B`
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1).replace(/\.0$/, "")}K`
  return n.toString()
}

export function formatTotalDuration(sec: number): string {
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  if (h >= 100) return `${h}h`
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m`
  return `${Math.floor(sec)}s`
}

export function formatMonthYear(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    month: "short",
    year: "numeric",
  })
}

export function formatDomain(url: string): string {
  try {
    const u = new URL(url)
    return u.hostname.replace(/^www\./, "")
  } catch {
    return url
  }
}

export function formatGb(gb: number): string {
  if (gb >= 1000) return `${(gb / 1000).toFixed(2)} TB`
  if (gb >= 10) return `${Math.round(gb)} GB`
  if (gb >= 1) return `${gb.toFixed(1)} GB`
  return `${Math.round(gb * 1000)} MB`
}

export function formatGbPerHour(gb: number): string {
  if (gb < 1) return `${Math.round(gb * 1000)} MB / hr`
  return `${gb.toFixed(2)} GB / hr`
}

export function formatHours(hours: number): string {
  if (hours >= 100) return `${Math.round(hours)} h`
  if (hours >= 10) return `${Math.round(hours)} h`
  return `${hours.toFixed(1)} h`
}

export function formatUsd(amount: number): string {
  if (amount === 0) return "$0.00"
  if (amount < 0.01) return "< $0.01"
  if (amount < 100) return `$${amount.toFixed(2)}`
  if (amount < 1000) return `$${amount.toFixed(0)}`
  return `$${Math.round(amount).toLocaleString()}`
}

/**
 * Bytes for humans, in DECIMAL units: 1 GB = 1,000,000,000 bytes.
 *
 * Deliberately not 1024. Backblaze bills per decimal GB and the backend
 * agrees (billing.py BYTES_PER_GB), so dividing by 1024 here printed
 * every storage figure a customer sees 7.4% BELOW the number they are
 * charged for. The home page read 3.37 GB while the invoice counted
 * 3.62 - invisible at a few gigabytes, and indefensible on a bill.
 *
 * Everything else in the app was already decimal (estimates.ts, the
 * admin panel's own copy of this function). This was the last holdout,
 * and it was the one on the pages customers actually read.
 */
export function formatBytes(b: number): string {
  if (b === 0) return "0 B"
  if (b < 1_000) return `${b} B`
  if (b < 1_000_000) return `${(b / 1_000).toFixed(1)} KB`
  if (b < 1_000_000_000) return `${(b / 1_000_000).toFixed(1)} MB`
  if (b < 1_000_000_000_000) return `${(b / 1_000_000_000).toFixed(2)} GB`
  return `${(b / 1_000_000_000_000).toFixed(2)} TB`
}

export function formatRelativeDate(iso: string): string {
  const then = new Date(iso).getTime()
  const now = Date.now()
  const diffSec = (now - then) / 1000
  const day = 86400
  if (diffSec < 60) return "just now"
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`
  if (diffSec < day) return `${Math.floor(diffSec / 3600)}h ago`
  if (diffSec < 2 * day) return "yesterday"
  if (diffSec < 7 * day) return `${Math.floor(diffSec / day)} days ago`
  if (diffSec < 30 * day) return `${Math.floor(diffSec / (7 * day))} weeks ago`
  if (diffSec < 365 * day) return `${Math.floor(diffSec / (30 * day))} months ago`
  return `${Math.floor(diffSec / (365 * day))} years ago`
}

export function formatFullDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  })
}

/**
 * Human "time remaining until" a future ISO timestamp, expressed in the
 * largest sensible whole unit ("29 days", "5 hours", "12 minutes").
 * Floors to whole units. Returns "0 minutes" once the target is in the
 * past. Mirror of formatRelativeDate for forward-looking dates.
 */
export function formatTimeUntil(iso: string): string {
  const target = new Date(iso).getTime()
  const diffSec = (target - Date.now()) / 1000
  if (diffSec <= 0) return "0 minutes"
  const day = 86400
  if (diffSec >= day) {
    const days = Math.floor(diffSec / day)
    return `${days} ${days === 1 ? "day" : "days"}`
  }
  if (diffSec >= 3600) {
    const hours = Math.floor(diffSec / 3600)
    return `${hours} ${hours === 1 ? "hour" : "hours"}`
  }
  const minutes = Math.max(1, Math.floor(diffSec / 60))
  return `${minutes} ${minutes === 1 ? "minute" : "minutes"}`
}


/**
 * Monthly cost, as money people actually recognise.
 *
 * Anything stored costs at least a cent as far as the display is
 * concerned: "<$0.01/mo" is technically precise and reads like a glitch,
 * and rounding a real charge down to $0.00 would imply free. So a stored
 * byte rounds UP to $0.01 and only a genuinely empty channel shows $0.00.
 *
 * Rounding up also errs in the honest direction - we never understate
 * what something costs.
 */
export function formatMonthlyCost(usd: number | null | undefined): string {
  if (usd == null || usd <= 0) return "$0.00/mo"
  if (usd < 0.01) return "$0.01/mo"
  return `$${usd.toFixed(2)}/mo`
}
