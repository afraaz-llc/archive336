import { Link } from "react-router-dom"
import { AlertTriangle } from "lucide-react"
import type { Channel } from "@/lib/types"
import { ChannelAvatar } from "./ChannelAvatar"
import { Card } from "./ui/card"
import { Badge } from "./ui/badge"
import { Switch } from "./ui/switch"
import { cn } from "@/lib/utils"

type Props = {
  channel: Channel
  onToggleActive?: (channelId: string) => void
}

export function ChannelCard({ channel, onToggleActive }: Props) {
  const active = channel.settings.active
  const terminated = channel.youtubeStatus === "terminated"

  return (
    <Link to={`/youtube/channel/${channel.id}`} className="block group">
      <Card
        className={cn(
          "p-5 h-full",
          terminated && "border-red-500",
          !active && "opacity-50"
        )}
      >
        <div className="flex items-center gap-4">
          {channel.settings.saveChannelAvatar && (
            <ChannelAvatar
              url={channel.avatarUrl}
              name={channel.name || channel.handle || ""}
              size="size-14"
              textClassName="text-lg"
            />
          )}
          <div className="min-w-0 flex-1">
            {channel.settings.saveChannelAbout ? (
              <>
                <div className="font-semibold truncate">{channel.name}</div>
                <div className="text-xs text-muted-foreground font-mono truncate">
                  {channel.handle}
                </div>
              </>
            ) : (
              <div className="font-semibold truncate">{channel.handle}</div>
            )}
            {/*
              Deliberately non-committal. This flag flips after two failed
              scrapes of the channel's /about page, and a timeout, a bot
              interstitial, or a markup change is indistinguishable from a
              real ban. We can say we could not see the channel; we cannot
              say YouTube terminated it.
            */}
            {terminated && (
              <Badge variant="destructive" className="mt-1.5">
                <AlertTriangle />
                Unavailable on YouTube
              </Badge>
            )}
            {channel.pubsubLive && !terminated && (
              <div
                className="mt-1.5 inline-flex items-center gap-1.5 text-[10px] uppercase tracking-wider font-bold text-emerald-500"
                title="Subscribed to YouTube's PubSubHubbub feed - new uploads land in seconds."
              >
                <span className="inline-block size-1.5 rounded-full bg-emerald-500" />
                Live
              </div>
            )}
          </div>
          <div
            className="shrink-0 opacity-100"
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
      </Card>
    </Link>
  )
}
