import { Link } from "react-router-dom"
import { AlertTriangle } from "lucide-react"
import type { Channel } from "@/lib/types"
import { ChannelAvatar } from "./ChannelAvatar"
import { Switch } from "./ui/switch"
import { formatBytes, formatMonthlyCost } from "@/lib/format"

/**
 * One channel as a scannable row.
 *
 * This is where the numbers stripped from the cards belong. On a card,
 * storage and cost are decoration you have to stop and parse; in a
 * column you can sort by, they are the reason you opened the page. The
 * grid stays the way in to a channel, this is the way to compare them.
 */
export function ChannelListRow({
  channel,
  onToggleActive,
}: {
  channel: Channel
  onToggleActive?: (channelId: string) => void
}) {
  const active = channel.settings.active
  const terminated = channel.youtubeStatus === "terminated"
  const archived = channel.archivedVideoCount ?? 0
  const known = channel.knownVideoCount ?? channel.videoCount
  const bytes = channel.bytesStored ?? 0
  const cost = channel.projectedMonthlyCostUsd ?? 0

  return (
    <Link
      to={`/youtube/channel/${encodeURIComponent(channel.id)}`}
      className="block border border-border hover:border-muted-foreground"
    >
      <div className="flex items-center gap-4 px-4 h-14">
        <div className="shrink-0">
          <ChannelAvatar
            url={channel.avatarUrl}
            name={channel.name || channel.handle}
            size="size-8"
            textClassName="text-xs"
          />
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-sm font-semibold truncate flex items-center gap-1.5">
            {channel.name}
            {terminated && (
              <AlertTriangle className="size-3.5 text-destructive shrink-0" />
            )}
          </div>
          <div className="text-xs text-muted-foreground truncate">
            {channel.handle}
          </div>
        </div>

        {/* Fixed-width numeric columns so the values line up down the
            list. tabular-nums matters more here than anywhere else in
            the app: a column you cannot scan is just a card again. */}
        {/* The count carries the emphasis and the total is secondary,
            but secondary has to stay readable. These were dimmed to 40%
            of a grey that is already 60% of white, which lands around
            2:1 against a black row - the total and the slash were close
            to invisible. The difference is in the colour now, not in
            whether you can see it. */}
        <div className="hidden md:block w-24 text-right font-mono tabular-nums text-xs">
          {archived}
          <span className="text-muted-foreground"> / {known}</span>
        </div>
        <div className="hidden md:block w-20 text-right font-mono tabular-nums text-xs">
          {bytes > 0 ? (
            formatBytes(bytes)
          ) : (
            <span className="text-muted-foreground">—</span>
          )}
        </div>
        <div className="hidden lg:block w-24 text-right font-mono tabular-nums text-xs text-muted-foreground">
          {/* formatMonthlyCost already carries the /mo suffix. */}
          {cost > 0 ? formatMonthlyCost(cost) : "—"}
        </div>
        <div
          className="w-16 shrink-0 flex justify-end"
          onClick={(e) => {
            e.preventDefault()
            e.stopPropagation()
          }}
        >
          <Switch
            checked={active}
            onCheckedChange={() => onToggleActive?.(channel.id)}
            aria-label={`${channel.name} active`}
          />
        </div>
      </div>
    </Link>
  )
}
