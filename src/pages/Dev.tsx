import * as React from "react"
import { Navigate } from "react-router-dom"
import { AlertTriangle, Wrench } from "lucide-react"
import { useAuth } from "@/auth/AuthContext"
import { formatRelativeDate } from "@/lib/format"
import { useDocumentTitle } from "@/lib/useDocumentTitle"
import {
  DEV_OVERLAY_CHANGE_EVENT,
  getDevOverlayVisible,
  setDevOverlayVisible,
} from "@/lib/paymentStatus"
import { Switch } from "@/components/ui/switch"

type Tab = "tools" | "errors"

type ErrorRow = {
  id: string
  userId: string | null
  username: string | null
  email: string | null
  source: string
  message: string
  stack: string | null
  // True when the list endpoint trimmed the stack to its 2 KB budget.
  // The expanded row fetches /api/admin/errors/{id} for the full trace.
  stackTruncated: boolean
  requestPath: string | null
  requestMethod: string | null
  statusCode: number | null
  userAgent: string | null
  createdAt: string | null
}

type ErrorListResponse = {
  total: number
  limit: number
  offset: number
  items: ErrorRow[]
}

export default function Dev() {
  useDocumentTitle("Dev")
  const { state } = useAuth()
  const [tab, setTab] = React.useState<Tab>("tools")

  if (state.status !== "authed") return <Navigate to="/" replace />
  if (!state.user.is_admin) return <Navigate to="/" replace />

  return (
    <div className="admin-selectable p-8 max-w-5xl mx-auto">
      <h1 className="text-2xl font-extrabold tracking-tight">Dev</h1>

      <div className="mt-6 border-b border-border flex gap-0">
        <TabButton active={tab === "tools"} onClick={() => setTab("tools")}>
          <Wrench className="size-4" />
          Tools
        </TabButton>
        <TabButton active={tab === "errors"} onClick={() => setTab("errors")}>
          <AlertTriangle className="size-4" />
          Errors
        </TabButton>
      </div>

      <div className="mt-8">
        {tab === "tools" && <ToolsTab />}
        {tab === "errors" && <ErrorsTab />}
      </div>
    </div>
  )
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={
        "flex items-center gap-2 px-4 py-2.5 text-sm font-semibold cursor-pointer border-b-2 -mb-px " +
        (active
          ? "border-foreground text-foreground"
          : "border-transparent text-muted-foreground hover:text-foreground")
      }
    >
      {children}
    </button>
  )
}

// ------------------------------------------------------------------
// Tools tab - the toggle for the floating payment-simulator overlay.
// The actual Real/HasCard/NoCard pills now live in DevPaymentOverlay,
// rendered into the bottom-right of every page from AppShell. This
// tab just controls whether that overlay shows up.
// ------------------------------------------------------------------

function ToolsTab() {
  const [overlayVisible, setVisible] = React.useState<boolean>(() =>
    getDevOverlayVisible()
  )

  React.useEffect(() => {
    const sync = () => setVisible(getDevOverlayVisible())
    window.addEventListener(DEV_OVERLAY_CHANGE_EVENT, sync)
    window.addEventListener("storage", sync)
    return () => {
      window.removeEventListener(DEV_OVERLAY_CHANGE_EVENT, sync)
      window.removeEventListener("storage", sync)
    }
  }, [])

  const toggle = (v: boolean) => {
    setDevOverlayVisible(v)
    setVisible(v)
  }

  return (
    <div className="space-y-8">
      <section>
        <SectionHeader>Payment status simulator</SectionHeader>
        <div className="flex items-start justify-between gap-4 border border-border p-4">
          <div className="min-w-0 flex-1">
            <div className="text-sm font-semibold">Show overlay</div>
            <p className="text-xs text-muted-foreground mt-1 leading-relaxed">
              Floats a Real / Has card / No card pill in the bottom-right
              corner of every page. Picking a status forces every
              payment-aware piece of UI (Sidebar settings icon, Plan
              card, Paywalled wrappers) to render as if the current user
              had it. Real Stripe state isn't touched.
            </p>
          </div>
          <div className="mt-0.5">
            <Switch
              checked={overlayVisible}
              onCheckedChange={toggle}
              aria-label="Show payment simulator overlay"
            />
          </div>
        </div>
      </section>
    </div>
  )
}

// ------------------------------------------------------------------
// Errors tab - paginated table of captured errors with row expansion
// to show the full stack trace and request context for one error.
// ------------------------------------------------------------------

function ErrorsTab() {
  const [data, setData] = React.useState<ErrorListResponse | null>(null)
  const [error, setError] = React.useState<string | null>(null)
  const [source, setSource] = React.useState<"all" | "server" | "client">(
    "all"
  )
  const [expandedId, setExpandedId] = React.useState<string | null>(null)

  const refresh = React.useCallback(async () => {
    try {
      const url =
        source === "all"
          ? "/api/admin/errors?limit=100"
          : `/api/admin/errors?source=${source}&limit=100`
      const r = await fetch(url, { credentials: "include" })
      if (!r.ok) {
        setError(`HTTP ${r.status}`)
        return
      }
      const json = (await r.json()) as ErrorListResponse
      setData(json)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed")
    }
  }, [source])

  React.useEffect(() => {
    void refresh()
  }, [refresh])

  if (error)
    return (
      <div className="text-sm text-destructive">Couldn't load: {error}</div>
    )
  if (!data)
    return <div className="text-sm text-muted-foreground">Loading...</div>

  return (
    <div>
      <div className="flex items-center gap-2 mb-4">
        <FilterPill
          active={source === "all"}
          onClick={() => setSource("all")}
        >
          All
        </FilterPill>
        <FilterPill
          active={source === "server"}
          onClick={() => setSource("server")}
        >
          Server
        </FilterPill>
        <FilterPill
          active={source === "client"}
          onClick={() => setSource("client")}
        >
          Client
        </FilterPill>
        <button
          type="button"
          onClick={() => void refresh()}
          className="text-xs text-muted-foreground hover:text-foreground cursor-pointer ml-2"
        >
          Refresh
        </button>
        <div className="text-xs text-muted-foreground ml-auto">
          {data.items.length} of {data.total}
        </div>
      </div>

      {data.items.length === 0 ? (
        <div className="border border-border p-8 text-center text-sm text-muted-foreground">
          No errors captured yet. Either everything's working or nothing's
          happening.
        </div>
      ) : (
        <div className="border border-border">
          <table className="w-full text-sm">
            <thead className="bg-muted/30 text-[10px] uppercase tracking-wider text-muted-foreground">
              <tr>
                <Th>When</Th>
                <Th>Source</Th>
                <Th>User</Th>
                <Th>Where</Th>
                <Th>Message</Th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((row) => {
                const expanded = expandedId === row.id
                return (
                  <React.Fragment key={row.id}>
                    <tr
                      className="border-t border-border hover:bg-muted/20 cursor-pointer"
                      onClick={() =>
                        setExpandedId(expanded ? null : row.id)
                      }
                    >
                      <Td className="text-xs text-muted-foreground whitespace-nowrap">
                        {row.createdAt
                          ? formatRelativeDate(row.createdAt)
                          : "—"}
                      </Td>
                      <Td>
                        <SourceBadge source={row.source} />
                      </Td>
                      <Td className="text-xs">
                        {row.username ? (
                          <div>
                            <div className="font-semibold">{row.username}</div>
                            <div className="font-mono text-muted-foreground">
                              {row.email}
                            </div>
                          </div>
                        ) : (
                          <span className="text-muted-foreground">
                            anonymous
                          </span>
                        )}
                      </Td>
                      <Td className="font-mono text-xs text-muted-foreground">
                        {row.requestMethod && row.requestPath
                          ? `${row.requestMethod} ${row.requestPath}`
                          : row.requestPath || "—"}
                      </Td>
                      <Td className="font-mono text-xs">
                        <div className="truncate max-w-md">{row.message}</div>
                      </Td>
                    </tr>
                    {expanded && (
                      <tr className="border-t border-border bg-muted/10">
                        <td colSpan={5} className="p-4">
                          <ExpandedDetail row={row} />
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function ExpandedDetail({ row }: { row: ErrorRow }) {
  // When the list endpoint truncated this row's stack to its 2 KB
  // budget, the expanded panel lets the operator pull the full trace
  // on demand. We don't auto-fetch on expand — many error inspections
  // don't need the full trace (most stacks are <2 KB anyway), and the
  // explicit click signals intent to the server about which traces
  // are actually being read.
  const [fullStack, setFullStack] = React.useState<string | null>(null)
  const [fetching, setFetching] = React.useState(false)
  const [fetchError, setFetchError] = React.useState<string | null>(null)

  const stackToShow = fullStack ?? row.stack
  const isStillTruncated = row.stackTruncated && fullStack === null

  const loadFullStack = async () => {
    setFetching(true)
    setFetchError(null)
    try {
      const r = await fetch(
        `/api/admin/errors/${encodeURIComponent(row.id)}`,
        { credentials: "include" }
      )
      if (!r.ok) {
        setFetchError(`HTTP ${r.status}`)
        return
      }
      const json = (await r.json()) as ErrorRow
      setFullStack(json.stack ?? "")
    } catch (e) {
      setFetchError(e instanceof Error ? e.message : "Failed")
    } finally {
      setFetching(false)
    }
  }

  return (
    <div className="space-y-3 text-xs">
      <DetailRow label="Message" value={row.message} mono />
      {row.statusCode !== null && (
        <DetailRow label="Status" value={String(row.statusCode)} />
      )}
      {row.userAgent && <DetailRow label="User agent" value={row.userAgent} />}
      {row.userId && <DetailRow label="User id" value={row.userId} mono />}
      {stackToShow && (
        <div>
          <div className="flex items-center gap-3 mb-1">
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold">
              Stack
              {isStillTruncated && (
                <span className="ml-2 text-yellow-500 normal-case font-normal">
                  (truncated to 2 KB)
                </span>
              )}
              {fullStack !== null && (
                <span className="ml-2 text-muted-foreground normal-case font-normal">
                  (full)
                </span>
              )}
            </div>
            {isStillTruncated && (
              <button
                type="button"
                onClick={() => void loadFullStack()}
                disabled={fetching}
                className="text-[10px] uppercase tracking-wider font-bold cursor-pointer text-primary hover:text-primary/80 disabled:opacity-50"
              >
                {fetching ? "Loading..." : "View full stack"}
              </button>
            )}
            {fetchError && (
              <span className="text-[10px] text-destructive">
                Couldn't load: {fetchError}
              </span>
            )}
          </div>
          <pre className="font-mono text-[11px] leading-relaxed bg-background border border-border p-3 overflow-x-auto whitespace-pre-wrap break-all">
            {stackToShow}
          </pre>
        </div>
      )}
    </div>
  )
}

function DetailRow({
  label,
  value,
  mono,
}: {
  label: string
  value: string
  mono?: boolean
}) {
  return (
    <div className="flex gap-3">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold shrink-0 w-24 pt-0.5">
        {label}
      </div>
      <div className={"flex-1 " + (mono ? "font-mono break-all" : "")}>
        {value}
      </div>
    </div>
  )
}

function FilterPill({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={
        "px-2.5 py-1 cursor-pointer border text-xs uppercase tracking-wider font-bold " +
        (active
          ? "bg-foreground text-background border-foreground"
          : "border-border text-muted-foreground hover:text-foreground")
      }
    >
      {children}
    </button>
  )
}

function SourceBadge({ source }: { source: string }) {
  const cls =
    source === "server"
      ? "text-destructive border-destructive/40"
      : "text-primary border-primary/40"
  return (
    <span
      className={
        "inline-block text-[10px] uppercase tracking-wider font-bold px-1.5 py-0.5 border " +
        cls
      }
    >
      {source}
    </span>
  )
}

// ------------------------------------------------------------------
// Helpers shared across tabs.
// ------------------------------------------------------------------

function SectionHeader({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">
      {children}
    </div>
  )
}

function Th({
  children,
  className,
}: {
  children: React.ReactNode
  className?: string
}) {
  return (
    <th
      className={"text-left font-semibold px-3 py-2 " + (className ?? "")}
    >
      {children}
    </th>
  )
}

function Td({
  children,
  className,
}: {
  children: React.ReactNode
  className?: string
}) {
  return <td className={"px-3 py-2 align-top " + (className ?? "")}>{children}</td>
}
