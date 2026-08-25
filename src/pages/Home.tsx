import * as React from "react"
import { Link } from "react-router-dom"
import {
  Archive,
  CheckCircle2,
  Clock,
  CreditCard,
  HardDrive,
  Mail,
  MonitorPlay,
} from "lucide-react"
import type { Channel, Video } from "@/lib/types"
import { formatBytes, formatRelativeDate } from "@/lib/format"
import { useAuth } from "@/auth/AuthContext"
import { SetupAlert } from "@/components/SetupAlert"
import { WorkerStatusCard } from "@/components/WorkerStatusCard"
import { useWorkerStatus } from "@/lib/workerStatus"
import { ChannelAvatar } from "@/components/ChannelAvatar"
import {
  BILLING_CHANGE_EVENT,
  hasKnownPaymentStatus,
  readHasPaymentMethod,
  refreshBillingStatus,
} from "@/lib/paymentStatus"

export default function Home() {
  const [channels, setChannels] = React.useState<Channel[] | null>(null)
  const [videos, setVideos] = React.useState<Video[]>([])

  // Account-level onboarding signals for the alerts at the top of the
  // dashboard. email_verified rides on the auth user; payment status
  // comes from the shared billing cache (which we warm on mount below,
  // since the cache is otherwise only filled once the user opens Billing).
  const { state } = useAuth()
  const user = state.status === "authed" ? state.user : null
  const [hasCard, setHasCard] = React.useState<boolean>(() =>
    readHasPaymentMethod()
  )
  // Whether billing has actually been checked yet. Gates the "no payment
  // method" alert so it never flashes on a cold login (empty cache reads
  // as no-card) before /api/billing/status confirms the real state.
  const [billingKnown, setBillingKnown] = React.useState<boolean>(() =>
    hasKnownPaymentStatus()
  )

  React.useEffect(() => {
    const sync = () => {
      setHasCard(readHasPaymentMethod())
      setBillingKnown(hasKnownPaymentStatus())
    }
    window.addEventListener(BILLING_CHANGE_EVENT, sync)
    void refreshBillingStatus()
    return () => window.removeEventListener(BILLING_CHANGE_EVENT, sync)
  }, [])

  React.useEffect(() => {
    let cancelled = false

    // Walk the cursor-paginated /videos endpoint until exhausted. Used
    // per-channel below. We only care about identity + status + bytes
    // here (for the dashboard counters); thumbnails aren't rendered so
    // we don't bother bulk-presigning them.
    const loadAllVideosForChannel = async (
      channelId: string
    ): Promise<Video[]> => {
      const out: Video[] = []
      let cursor: string | null = null
      // eslint-disable-next-line no-constant-condition
      while (true) {
        const url = new URL(
          `/api/youtube/channels/${encodeURIComponent(channelId)}/videos`,
          window.location.origin
        )
        if (cursor) url.searchParams.set("cursor", cursor)
        url.searchParams.set("limit", "200")
        const r = await fetch(url.toString(), { credentials: "include" })
        if (!r.ok) return out
        const body = (await r.json()) as {
          items?: Video[]
          nextCursor?: string | null
        }
        if (!body || !Array.isArray(body.items)) return out
        out.push(...body.items)
        if (!body.nextCursor) break
        cursor = body.nextCursor
      }
      return out
    }

    void (async () => {
      try {
        const channelsRes = await fetch("/api/youtube/channels", {
          credentials: "include",
        })
        if (cancelled) return
        if (channelsRes.ok) {
          const cs = (await channelsRes.json()) as Channel[]
          setChannels(cs)
          // Fetch videos for each channel in parallel.
          const allVideos = await Promise.all(
            cs.map((c) => loadAllVideosForChannel(c.id))
          )
          if (!cancelled) setVideos(allVideos.flat())
        }
      } catch {
        // Silent — empty state if anything fails
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  const archived = videos.filter(
    (v) => v.status === "archived" || (v.localPath !== null && v.fileSizeBytes !== null)
  )
  const archivedCount = archived.length
  const totalBytes = archived.reduce((acc, v) => acc + (v.fileSizeBytes ?? 0), 0)
  const latestArchived = archived
    .map((v) => v.archivedAt)
    .filter((d): d is string => !!d)
    .sort()
    .reverse()[0]

  // The dashboard is where "is my backup running" belongs, so this page
  // polls for it. Home is the landing page; a status that only appeared on
  // /youtube was a footnote on a page the user might never open.
  const worker = useWorkerStatus(true)

  const needsEmailVerify = user !== null && !user.email_verified
  const needsPaymentMethod = billingKnown && !hasCard

  return (
    <div className="p-8 max-w-4xl mx-auto">
      {/* Backup app health, first thing on the page. Only shown once the
          user actually has channels - before that the setup alerts below
          are the right next step, and a "not running" warning would be
          noise about an app they have no reason to have installed yet. */}
      {channels !== null && channels.length > 0 && (
        <div className="mt-8">
          <WorkerStatusCard worker={worker} />
        </div>
      )}

      {/* Account setup alerts — shown until email is verified and a
          payment method is on file. Each routes into the relevant
          Settings section. */}
      {(needsEmailVerify || needsPaymentMethod) && (
        <div className="mt-8 space-y-2">
          {needsEmailVerify && (
            <SetupAlert
              icon={<Mail className="size-4" />}
              title="Verify your email address"
              to="/settings?tab=account"
            />
          )}
          {needsPaymentMethod && (
            <SetupAlert
              icon={<CreditCard className="size-4" />}
              title="Add a payment method"
              to="/settings#payment"
            />
          )}
        </div>
      )}

      {/* Loading state */}
      {channels === null && (
        <div className="mt-10 text-sm text-muted-foreground">Loading…</div>
      )}

      {/* Has channels — show stats */}
      {channels !== null && channels.length > 0 && (
        <>
          <div className="mt-8 grid grid-cols-2 sm:grid-cols-4 gap-3">
            <Stat
              icon={<MonitorPlay className="size-4" />}
              label="Channels"
              value={String(channels.length)}
            />
            <Stat
              icon={<Archive className="size-4" />}
              label="Videos"
              value={String(videos.length)}
            />
            <Stat
              icon={<CheckCircle2 className="size-4" />}
              label="Archived"
              value={`${archivedCount} / ${videos.length}`}
            />
            <Stat
              icon={<HardDrive className="size-4" />}
              label="Storage"
              value={formatBytes(totalBytes)}
            />
          </div>

          {latestArchived && (
            <div className="mt-3 flex items-center gap-2 text-xs text-muted-foreground">
              <Clock className="size-3" />
              Last archived {formatRelativeDate(latestArchived)}
            </div>
          )}

          <div className="mt-8">
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">
              Channels
            </div>
            <div className="space-y-2">
              {channels.map((c) => {
                const myVideos = videos.filter((v) => v.channelId === c.id)
                const myArchived = myVideos.filter(
                  (v) =>
                    v.status === "archived" ||
                    (v.localPath !== null && v.fileSizeBytes !== null)
                ).length
                return (
                  <Link
                    key={c.id}
                    to={`/youtube/channel/${c.id}`}
                    className="block border border-border p-4 cursor-pointer"
                  >
                    <div className="flex items-center gap-4">
                      <ChannelAvatar
                        url={c.avatarUrl}
                        name={c.name || c.handle || ""}
                        size="size-12"
                        textClassName="text-base"
                      />
                      <div className="min-w-0 flex-1">
                        <div className="font-semibold text-sm">{c.name}</div>
                        <div className="text-xs text-muted-foreground font-mono mt-0.5">
                          {c.handle}
                        </div>
                      </div>
                      <div className="text-xs text-muted-foreground text-right shrink-0 font-mono tabular-nums">
                        {myArchived} / {myVideos.length} archived
                      </div>
                    </div>
                  </Link>
                )
              })}
            </div>
          </div>
        </>
      )}
    </div>
  )
}

function Stat({
  icon,
  label,
  value,
}: {
  icon: React.ReactNode
  label: string
  value: string
}) {
  return (
    <div className="border border-border p-4">
      <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-muted-foreground font-semibold">
        {icon}
        <span>{label}</span>
      </div>
      <div className="mt-1.5 text-lg font-bold font-mono tabular-nums">
        {value}
      </div>
    </div>
  )
}
