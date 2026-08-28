import * as React from "react"
import { Navigate } from "react-router-dom"
import {
  Activity,
  CheckCircle2,
  CreditCard,
  Database,
  DollarSign,
  ExternalLink,
  HardDrive,
  Layers,
  LineChart,
  Mail,
  Server,
  TrendingUp,
  Users as UsersIcon,
} from "lucide-react"
import { useAuth } from "@/auth/AuthContext"
import { formatBytes, formatRelativeDate } from "@/lib/format"
import { useDocumentTitle } from "@/lib/useDocumentTitle"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

type Tab =
  | "system"
  | "users"
  | "billing"
  | "pnl"
  | "memberships"
  | "expenses"
  | "live"
  | "stack"
  | "support"

const ADMIN_TABS: readonly Tab[] = [
  "system",
  "users",
  "billing",
  "pnl",
  "memberships",
  "expenses",
  "live",
  "stack",
  "support",
] as const

// Read the active tab from the URL hash (e.g. /admin#stack → "stack").
// Falls back to "system" for missing or unrecognized hashes.
function readTabFromHash(): Tab {
  if (typeof window === "undefined") return "system"
  const raw = window.location.hash.replace(/^#/, "")
  return (ADMIN_TABS as readonly string[]).includes(raw) ? (raw as Tab) : "system"
}

type SystemMetrics = {
  users: {
    total: number
    emailVerified: number
    paying: number
    pastDue: number
    recentSignups: number
  }
  storage: {
    totalBytes: number
    totalGb: number
  }
  r2: {
    objects: number | null
    bytes: number | null
    monthlyCostUsd: number | null
  }
  resend: {
    sendsToday: number
    sendsThisMonth: number
    freeMonthlyLimit: number
    freeDailyLimit: number
  }
  billing: {
    unbilledGbDays: number
    unbilledUsd: number
  }
}

type AdminUser = {
  id: string
  username: string
  email: string
  emailVerified: boolean
  isAdmin: boolean
  paymentStatus: string
  stripeCustomerId: string | null
  createdAt: string
  lastSeenAt: string | null
  storageBytes: number
  // R2 ops driven by this user since the start of the current UTC
  // calendar month. Class A = writes/lists/multipart, Class B =
  // reads/heads. Used to surface ops usage alongside storage in
  // the Users tab so we can spot heavy users + verify the ledger.
  opsMonthA: number
  opsMonthB: number
}

type UserListResponse = {
  total: number
  limit: number
  offset: number
  items: AdminUser[]
}

type BillingInvoice = {
  id: string
  customerId: string | null
  username: string | null
  email: string | null
  amountUsd: number
  amountPaidUsd: number
  status: string
  description: string
  createdAt: string
  hostedInvoiceUrl: string | null
}

type BillingSnapshot = {
  subscriptions: {
    active: number
    pastDue: number
    trialing: number
    canceled: number
    mrrUsd: number
  }
  revenue: {
    last30dUsd: number
    last90dUsd: number
    lifetimeUsd: number
  }
  invoices: BillingInvoice[]
}

// Stripe webhook audit feed from /api/admin/stripe-audit-log.
// Every webhook event lands in stripe_audit_log with the full
// payload; this is the operator's view of what Stripe has been
// telling us.
type StripeAuditEvent = {
  id: string
  stripeEventId: string
  eventType: string
  receivedAt: string | null
  stripeCustomerId: string | null
  username: string | null
  email: string | null
  handled: boolean
  notes: string | null
}
type StripeAuditFeed = {
  events: StripeAuditEvent[]
  byTypeLast24h: Record<string, number>
  totalShown: number
}

type Pnl = {
  costs: {
    hetznerUsd: number
    litestreamAmortizedUsd: number
    fixedUsd: number
    r2Usd: number
    resendUsd: number
    totalUsd: number
  }
  // Business-ops costs are the real-world expenses that aren't part
  // of the product cost model (which lives in the Expenses tab). Stripe
  // fees pulled live from BalanceTransaction; everything else is a
  // $0 placeholder that activates when set up.
  businessOps: {
    stripeFeesUsd: number
    taxesUsd: number
    royaltiesUsd: number
    affiliateUsd: number
    customerSupportUsd: number
    totalUsd: number
    notes: {
      stripeFees?: string
      taxes?: string
      royalties?: string
      affiliate?: string
      customerSupport?: string
    }
    errors: string[]
  }
  revenue: {
    mrrUsd: number
    last30dUsd: number
  }
  netLast30dUsd: number
}

export default function Admin() {
  useDocumentTitle("Admin")
  const { state } = useAuth()
  const [tab, setTab] = React.useState<Tab>(() => readTabFromHash())

  // Keep the URL hash in sync with the active tab so refresh / direct
  // link / browser back-forward all land you on the same tab.
  // Using replaceState (not pushState) so tab switches don't pollute
  // the back-button history with /admin#system, /admin#users, etc.
  React.useEffect(() => {
    if (window.location.hash !== `#${tab}`) {
      window.history.replaceState(null, "", `#${tab}`)
    }
  }, [tab])

  // Pick up tab changes triggered by the browser (back/forward, or a
  // user editing the URL bar).
  React.useEffect(() => {
    const onHash = () => setTab(readTabFromHash())
    window.addEventListener("hashchange", onHash)
    return () => window.removeEventListener("hashchange", onHash)
  }, [])

  // Gate: only admins reach this page. Non-admins get bounced home.
  // RequireAuth already ensures we're logged in, so state.status here
  // is always "authed".
  if (state.status !== "authed") return <Navigate to="/" replace />
  if (!state.user.is_admin) return <Navigate to="/" replace />

  return (
    <div className="admin-selectable p-8 max-w-5xl mx-auto">
      <h1 className="text-2xl font-extrabold tracking-tight">Admin</h1>

      <div className="mt-6 border-b border-border flex gap-0">
        <TabButton active={tab === "system"} onClick={() => setTab("system")}>
          <Server className="size-4" />
          System
        </TabButton>
        <TabButton active={tab === "users"} onClick={() => setTab("users")}>
          <UsersIcon className="size-4" />
          Users
        </TabButton>
        <TabButton
          active={tab === "billing"}
          onClick={() => setTab("billing")}
        >
          <CreditCard className="size-4" />
          Billing
        </TabButton>
        <TabButton active={tab === "pnl"} onClick={() => setTab("pnl")}>
          <LineChart className="size-4" />
          P&amp;L
        </TabButton>
        <TabButton
          active={tab === "memberships"}
          onClick={() => setTab("memberships")}
        >
          <TrendingUp className="size-4" />
          Memberships
        </TabButton>
        <TabButton
          active={tab === "expenses"}
          onClick={() => setTab("expenses")}
        >
          <DollarSign className="size-4" />
          Expenses
        </TabButton>
        <TabButton active={tab === "live"} onClick={() => setTab("live")}>
          <Activity className="size-4" />
          Live
        </TabButton>
        <TabButton active={tab === "support"} onClick={() => setTab("support")}>
          Support
        </TabButton>
        <TabButton active={tab === "stack"} onClick={() => setTab("stack")}>
          <Layers className="size-4" />
          Stack
        </TabButton>
      </div>

      <div className="mt-8">
        {tab === "system" && <SystemTab />}
        {tab === "users" && <UsersTab />}
        {tab === "billing" && <BillingTab />}
        {tab === "pnl" && <PnlTab />}
        {tab === "memberships" && <MembershipsTab />}
        {tab === "expenses" && <ExpensesTab />}
        {tab === "live" && <LiveTab />}
        {tab === "stack" && <StackTab />}
        {tab === "support" && <SupportTab />}
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
// System tab
// ------------------------------------------------------------------

function SystemTab() {
  const [data, setData] = React.useState<SystemMetrics | null>(null)
  const [error, setError] = React.useState<string | null>(null)

  React.useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const r = await fetch("/api/admin/system", { credentials: "include" })
        if (!r.ok) {
          setError(`HTTP ${r.status}`)
          return
        }
        const json = (await r.json()) as SystemMetrics
        if (!cancelled) setData(json)
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load")
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  if (error)
    return (
      <div className="text-sm text-destructive">Couldn't load: {error}</div>
    )
  if (!data)
    return <div className="text-sm text-muted-foreground">Loading…</div>

  return (
    <div className="space-y-8">
      <section>
        <SectionHeader>Users</SectionHeader>
        <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
          <Stat label="Total" value={String(data.users.total)} />
          <Stat
            label="Email verified"
            value={String(data.users.emailVerified)}
          />
          <Stat label="Paying" value={String(data.users.paying)} />
          <Stat label="Past due" value={String(data.users.pastDue)} />
          <Stat
            label="Last 7 days"
            value={`+${data.users.recentSignups}`}
          />
        </div>
      </section>

      <section>
        <SectionHeader>Storage (DB)</SectionHeader>
        <div className="grid grid-cols-2 gap-3">
          <Stat
            icon={<HardDrive className="size-4" />}
            label="Archived"
            value={formatBytes(data.storage.totalBytes)}
          />
          <Stat
            icon={<Database className="size-4" />}
            label="GB on disk"
            value={data.storage.totalGb.toFixed(2)}
          />
        </div>
      </section>

      <section>
        <SectionHeader>Cloudflare R2 (live)</SectionHeader>
        <div className="grid grid-cols-3 gap-3">
          <Stat
            label="Objects"
            value={
              data.r2.objects === null
                ? "—"
                : data.r2.objects.toLocaleString()
            }
          />
          <Stat
            icon={<HardDrive className="size-4" />}
            label="Bytes in R2"
            value={
              data.r2.bytes === null ? "—" : formatBytes(data.r2.bytes)
            }
          />
          <Stat
            label="R2 cost / mo"
            value={
              data.r2.monthlyCostUsd === null
                ? "—"
                : `$${data.r2.monthlyCostUsd.toFixed(4)}`
            }
          />
        </div>
        {data.r2.bytes !== null && data.r2.bytes !== data.storage.totalBytes && (
          <p className="mt-3 text-xs text-destructive">
            Drift: R2 reports {formatBytes(data.r2.bytes)}, DB sums to{" "}
            {formatBytes(data.storage.totalBytes)}. Investigate orphan
            objects or failed uploads.
          </p>
        )}
        <p className="mt-3 text-xs text-muted-foreground">
          Backblaze charges $0.007/GB/month at rest; Basic users pay
          $0.020/GB/month. Egress is free via the Cloudflare proxy, and B2
          bills nothing for the transaction classes we use, so storage is
          the whole line.
        </p>
      </section>

      <section>
        <SectionHeader>Resend (transactional email)</SectionHeader>
        <div className="grid grid-cols-2 gap-3">
          <Stat
            icon={<Mail className="size-4" />}
            label="Sends today"
            value={`${data.resend.sendsToday} / ${data.resend.freeDailyLimit}`}
          />
          <Stat
            icon={<Mail className="size-4" />}
            label="Sends this month"
            value={`${data.resend.sendsThisMonth} / ${data.resend.freeMonthlyLimit}`}
          />
        </div>
        {(data.resend.sendsToday >= data.resend.freeDailyLimit * 0.8 ||
          data.resend.sendsThisMonth >=
            data.resend.freeMonthlyLimit * 0.8) && (
          <p className="mt-3 text-xs text-destructive">
            Approaching the Resend free-tier ceiling. Either upgrade or
            audit which sends are necessary.
          </p>
        )}
        <p className="mt-3 text-xs text-muted-foreground">
          Counted from our own send log so the number matches reality
          even though the API key is send-only scope.
        </p>
      </section>

      <section>
        <SectionHeader>Unbilled storage</SectionHeader>
        <div className="grid grid-cols-2 gap-3">
          <Stat
            label="GB-days queued"
            value={data.billing.unbilledGbDays.toFixed(2)}
          />
          <Stat
            label="USD queued"
            value={`$${data.billing.unbilledUsd.toFixed(4)}`}
          />
        </div>
        <p className="mt-3 text-xs text-muted-foreground">
          Queued amounts roll into the next 3rd-of-month billing run for any
          user whose unbilled total has crossed the $0.50 threshold.
        </p>
      </section>
    </div>
  )
}

// ------------------------------------------------------------------
// Users tab
// ------------------------------------------------------------------

function UsersTab() {
  const [q, setQ] = React.useState("")
  const [data, setData] = React.useState<UserListResponse | null>(null)
  const [error, setError] = React.useState<string | null>(null)
  const [loading, setLoading] = React.useState(true)

  // Debounce search so we don't hammer the backend on every keystroke.
  const debouncedQ = useDebounced(q, 300)

  React.useEffect(() => {
    let cancelled = false
    setLoading(true)
    void (async () => {
      try {
        const url = debouncedQ
          ? `/api/admin/users?q=${encodeURIComponent(debouncedQ)}`
          : "/api/admin/users"
        const r = await fetch(url, { credentials: "include" })
        if (!r.ok) {
          setError(`HTTP ${r.status}`)
          return
        }
        const json = (await r.json()) as UserListResponse
        if (!cancelled) {
          setData(json)
          setError(null)
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed")
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [debouncedQ])

  return (
    <div>
      <div className="flex items-center gap-3 mb-4">
        <input
          type="text"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search username or email"
          className="flex-1 max-w-sm h-9 px-3 border border-border bg-background text-sm"
        />
        {data && (
          <div className="text-xs text-muted-foreground">
            {data.items.length} of {data.total}
          </div>
        )}
      </div>

      {error && (
        <div className="text-sm text-destructive">Couldn't load: {error}</div>
      )}

      {loading && !data && (
        <div className="text-sm text-muted-foreground">Loading…</div>
      )}

      {data && data.items.length === 0 && !loading && (
        <div className="text-sm text-muted-foreground">No users match.</div>
      )}

      {data && data.items.length > 0 && (
        <div className="border border-border">
          <table className="w-full text-sm">
            <thead className="bg-muted/30 text-[10px] uppercase tracking-wider text-muted-foreground">
              <tr>
                <Th>User</Th>
                <Th>Email</Th>
                <Th>Status</Th>
                <Th className="text-right">Storage</Th>
                <Th className="text-right" title="R2 Class A ops (writes/lists/multipart) this month">Ops A·mo</Th>
                <Th className="text-right" title="R2 Class B ops (reads/heads) this month">Ops B·mo</Th>
                <Th>Joined</Th>
                <Th>Last seen</Th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((u) => (
                <tr
                  key={u.id}
                  className="border-t border-border hover:bg-muted/20"
                >
                  <Td>
                    <div className="font-semibold flex items-center gap-1.5">
                      {u.username}
                      {u.isAdmin && (
                        <span className="text-[9px] uppercase tracking-wider font-bold text-primary border border-primary/40 px-1">
                          Admin
                        </span>
                      )}
                    </div>
                  </Td>
                  <Td>
                    <div className="flex items-center gap-1.5">
                      <span className="font-mono text-xs">{u.email}</span>
                      {u.emailVerified && (
                        <CheckCircle2
                          className="size-3.5 text-primary"
                          aria-label="Verified"
                        />
                      )}
                    </div>
                  </Td>
                  <Td>
                    <PaymentBadge status={u.paymentStatus} />
                  </Td>
                  <Td className="text-right font-mono tabular-nums text-xs">
                    {u.storageBytes > 0
                      ? formatBytes(u.storageBytes)
                      : "—"}
                  </Td>
                  <Td className="text-right font-mono tabular-nums text-xs">
                    {u.opsMonthA > 0 ? u.opsMonthA.toLocaleString() : "—"}
                  </Td>
                  <Td className="text-right font-mono tabular-nums text-xs">
                    {u.opsMonthB > 0 ? u.opsMonthB.toLocaleString() : "—"}
                  </Td>
                  <Td className="text-xs text-muted-foreground">
                    {formatRelativeDate(u.createdAt)}
                  </Td>
                  <Td className="text-xs text-muted-foreground">
                    {u.lastSeenAt ? formatRelativeDate(u.lastSeenAt) : "never"}
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function PaymentBadge({ status }: { status: string }) {
  const map: Record<string, { label: string; cls: string }> = {
    active: {
      label: "Active",
      cls: "text-primary border-primary/40",
    },
    past_due: {
      label: "Past due",
      cls: "text-destructive border-destructive/40",
    },
    canceled: {
      label: "Canceled",
      cls: "text-muted-foreground border-border",
    },
    none: {
      label: "No card",
      cls: "text-muted-foreground border-border",
    },
  }
  const m = map[status] ?? {
    label: status,
    cls: "text-muted-foreground border-border",
  }
  return (
    <span
      className={
        "inline-block text-[10px] uppercase tracking-wider font-bold px-1.5 py-0.5 border " +
        m.cls
      }
    >
      {m.label}
    </span>
  )
}

// ------------------------------------------------------------------
// Billing tab — Stripe-side revenue + invoices, gated to admins via
// the /api/admin/billing endpoint. Live snapshot, no caching.
// ------------------------------------------------------------------

function BillingTab() {
  const [data, setData] = React.useState<BillingSnapshot | null>(null)
  const [error, setError] = React.useState<string | null>(null)
  const [audit, setAudit] = React.useState<StripeAuditFeed | null>(null)
  const [auditFilter, setAuditFilter] = React.useState<string>("")

  React.useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const r = await fetch("/api/admin/billing", { credentials: "include" })
        if (!r.ok) {
          const detail = await r.json().catch(() => ({}))
          setError(detail?.detail || `HTTP ${r.status}`)
          return
        }
        const json = (await r.json()) as BillingSnapshot
        if (!cancelled) setData(json)
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed")
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  // Refetch audit feed whenever the type filter changes. Separate
  // effect from the billing snapshot since they're independent.
  React.useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const params = new URLSearchParams({ limit: "50" })
        if (auditFilter) params.set("event_type", auditFilter)
        const r = await fetch(
          `/api/admin/stripe-audit-log?${params.toString()}`,
          { credentials: "include" },
        )
        if (!r.ok) return
        const json = (await r.json()) as StripeAuditFeed
        if (!cancelled) setAudit(json)
      } catch {
        // audit feed is supplemental - don't block the page on failure
      }
    })()
    return () => {
      cancelled = true
    }
  }, [auditFilter])

  if (error)
    return (
      <div className="text-sm text-destructive">Couldn't load: {error}</div>
    )
  if (!data)
    return <div className="text-sm text-muted-foreground">Loading…</div>

  return (
    <div className="space-y-8">
      <section>
        <SectionHeader>Subscriptions</SectionHeader>
        <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
          <Stat
            label="Active"
            value={String(data.subscriptions.active)}
          />
          <Stat
            label="Past due"
            value={String(data.subscriptions.pastDue)}
          />
          <Stat
            label="Trialing"
            value={String(data.subscriptions.trialing)}
          />
          <Stat
            label="Canceled"
            value={String(data.subscriptions.canceled)}
          />
          <Stat
            label="MRR"
            value={`$${data.subscriptions.mrrUsd.toFixed(2)}`}
          />
        </div>
        <p className="mt-3 text-xs text-muted-foreground">
          MRR is annual subscription revenue divided by 12. At $1/year per
          member, each active membership contributes ~$0.083/mo.
        </p>
      </section>

      <section>
        <SectionHeader>Revenue (paid invoices)</SectionHeader>
        <div className="grid grid-cols-3 gap-3">
          <Stat
            label="Last 30 days"
            value={`$${data.revenue.last30dUsd.toFixed(2)}`}
          />
          <Stat
            label="Last 90 days"
            value={`$${data.revenue.last90dUsd.toFixed(2)}`}
          />
          <Stat
            label="Lifetime"
            value={`$${data.revenue.lifetimeUsd.toFixed(2)}`}
          />
        </div>
      </section>

      <section>
        <SectionHeader>Recent invoices</SectionHeader>
        {data.invoices.length === 0 && (
          <div className="text-sm text-muted-foreground">
            No invoices yet.
          </div>
        )}
        {data.invoices.length > 0 && (
          <div className="border border-border">
            <table className="w-full text-sm">
              <thead className="bg-muted/30 text-[10px] uppercase tracking-wider text-muted-foreground">
                <tr>
                  <Th>Customer</Th>
                  <Th>Description</Th>
                  <Th>Status</Th>
                  <Th className="text-right">Amount</Th>
                  <Th>Created</Th>
                  <Th>Stripe</Th>
                </tr>
              </thead>
              <tbody>
                {data.invoices.map((inv) => (
                  <tr
                    key={inv.id}
                    className="border-t border-border hover:bg-muted/20"
                  >
                    <Td>
                      <div className="font-semibold">
                        {inv.username ?? "—"}
                      </div>
                      {inv.email && (
                        <div className="font-mono text-xs text-muted-foreground">
                          {inv.email}
                        </div>
                      )}
                    </Td>
                    <Td className="text-xs text-muted-foreground">
                      {inv.description || "—"}
                    </Td>
                    <Td>
                      <InvoiceStatusBadge status={inv.status} />
                    </Td>
                    <Td className="text-right font-mono tabular-nums text-xs">
                      ${inv.amountUsd.toFixed(2)}
                    </Td>
                    <Td className="text-xs text-muted-foreground">
                      {formatRelativeDate(inv.createdAt)}
                    </Td>
                    <Td>
                      {inv.hostedInvoiceUrl && (
                        <a
                          href={inv.hostedInvoiceUrl}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-primary hover:underline inline-flex items-center gap-1 text-xs"
                        >
                          <ExternalLink className="size-3" />
                          Open
                        </a>
                      )}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section>
        <SectionHeader>Webhook events (last 50)</SectionHeader>
        <p className="text-xs text-muted-foreground">
          Every Stripe webhook event is recorded forever in the
          stripe_audit_log table. Stripe's own Events API only retains
          for 30 days; this is the canonical history for dispute
          forensics. Replays are deduplicated via the stripe_event_id
          unique constraint.
        </p>

        {audit && Object.keys(audit.byTypeLast24h).length > 0 && (
          <div className="mt-3 flex flex-wrap gap-2">
            <button
              onClick={() => setAuditFilter("")}
              className={
                "text-[10px] uppercase tracking-wider font-bold px-2 py-1 border " +
                (auditFilter === ""
                  ? "border-primary text-primary"
                  : "border-border text-muted-foreground hover:border-foreground hover:text-foreground")
              }
            >
              All ({Object.values(audit.byTypeLast24h).reduce((a, b) => a + b, 0)})
            </button>
            {Object.entries(audit.byTypeLast24h)
              .sort((a, b) => b[1] - a[1])
              .map(([type, count]) => (
                <button
                  key={type}
                  onClick={() =>
                    setAuditFilter(auditFilter === type ? "" : type)
                  }
                  className={
                    "text-[10px] uppercase tracking-wider font-bold px-2 py-1 border font-mono " +
                    (auditFilter === type
                      ? "border-primary text-primary"
                      : "border-border text-muted-foreground hover:border-foreground hover:text-foreground")
                  }
                >
                  {type} · {count}
                </button>
              ))}
          </div>
        )}

        {audit && audit.events.length === 0 && (
          <div className="mt-3 text-sm text-muted-foreground">
            No events yet{auditFilter ? ` for type=${auditFilter}` : ""}.
          </div>
        )}

        {audit && audit.events.length > 0 && (
          <div className="mt-3 border border-border">
            <table className="w-full text-sm">
              <thead className="bg-muted/30 text-[10px] uppercase tracking-wider text-muted-foreground">
                <tr>
                  <Th>When</Th>
                  <Th>Event</Th>
                  <Th>User</Th>
                  <Th>Status</Th>
                  <Th>Notes</Th>
                  <Th>Stripe event ID</Th>
                </tr>
              </thead>
              <tbody>
                {audit.events.map((e) => (
                  <tr
                    key={e.id}
                    className="border-t border-border hover:bg-muted/20"
                  >
                    <Td className="text-xs text-muted-foreground whitespace-nowrap">
                      {e.receivedAt ? formatRelativeDate(e.receivedAt) : "—"}
                    </Td>
                    <Td className="font-mono text-xs">{e.eventType}</Td>
                    <Td className="text-xs">
                      {e.username ? (
                        <>
                          <div className="font-semibold">{e.username}</div>
                          {e.email && (
                            <div className="font-mono text-[10px] text-muted-foreground">
                              {e.email}
                            </div>
                          )}
                        </>
                      ) : e.stripeCustomerId ? (
                        <span className="font-mono text-[10px] text-muted-foreground">
                          {e.stripeCustomerId}
                        </span>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </Td>
                    <Td>
                      <span
                        className={
                          "inline-block text-[10px] uppercase tracking-wider font-bold px-1.5 py-0.5 border " +
                          (e.handled
                            ? "text-primary border-primary/40"
                            : "text-muted-foreground border-border")
                        }
                      >
                        {e.handled ? "Handled" : "Logged"}
                      </span>
                    </Td>
                    <Td className="text-xs text-muted-foreground">
                      {e.notes || "—"}
                    </Td>
                    <Td className="font-mono text-[10px] text-muted-foreground">
                      {e.stripeEventId}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  )
}

function InvoiceStatusBadge({ status }: { status: string }) {
  const map: Record<string, { label: string; cls: string }> = {
    paid: { label: "Paid", cls: "text-primary border-primary/40" },
    open: { label: "Open", cls: "text-muted-foreground border-border" },
    void: { label: "Void", cls: "text-muted-foreground border-border" },
    uncollectible: {
      label: "Uncollectible",
      cls: "text-destructive border-destructive/40",
    },
    draft: { label: "Draft", cls: "text-muted-foreground border-border" },
  }
  const m = map[status] ?? {
    label: status,
    cls: "text-muted-foreground border-border",
  }
  return (
    <span
      className={
        "inline-block text-[10px] uppercase tracking-wider font-bold px-1.5 py-0.5 border " +
        m.cls
      }
    >
      {m.label}
    </span>
  )
}

// ------------------------------------------------------------------
// P&L tab - cost roll-up + Stripe revenue + 30-day net. Single
// /api/admin/pnl endpoint does the math server-side.
// ------------------------------------------------------------------

function PnlTab() {
  const [data, setData] = React.useState<Pnl | null>(null)
  const [error, setError] = React.useState<string | null>(null)

  React.useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const r = await fetch("/api/admin/pnl", { credentials: "include" })
        if (!r.ok) {
          setError(`HTTP ${r.status}`)
          return
        }
        const json = (await r.json()) as Pnl
        if (!cancelled) setData(json)
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed")
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  if (error)
    return (
      <div className="text-sm text-destructive">Couldn't load: {error}</div>
    )
  if (!data)
    return <div className="text-sm text-muted-foreground">Loading...</div>

  const profit = data.netLast30dUsd >= 0
  const netLabel =
    (profit ? "+$" : "-$") + Math.abs(data.netLast30dUsd).toFixed(2)
  const productCosts = data.costs.totalUsd
  const businessOpsCosts = data.businessOps.totalUsd
  const totalAllCosts = productCosts + businessOpsCosts

  return (
    <div className="space-y-8">
      <section>
        <SectionHeader>Last 30 days</SectionHeader>
        <div className="grid grid-cols-3 gap-3">
          <Stat
            label="Revenue"
            value={`$${data.revenue.last30dUsd.toFixed(2)}`}
          />
          <Stat label="All costs" value={`$${totalAllCosts.toFixed(2)}`} />
          <div
            className={
              "border p-4 " +
              (profit
                ? "border-primary/40"
                : "border-destructive/40")
            }
          >
            <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-muted-foreground font-semibold">
              <span>Net</span>
            </div>
            <div
              className={
                "mt-1.5 text-lg font-bold font-mono tabular-nums " +
                (profit ? "text-primary" : "text-destructive")
              }
            >
              {netLabel}
            </div>
          </div>
        </div>
        <p className="mt-3 text-xs text-muted-foreground">
          Net is paid invoices in the last 30 days minus everything we paid
          for the same period (product costs + business-ops costs). Costs
          are stated as monthly run-rate so the comparison is apples-to-
          apples; treat this as "are unit economics in the right direction"
          rather than a strict accounting figure.
        </p>
      </section>

      <section>
        <SectionHeader>Product costs (monthly)</SectionHeader>
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
          <Stat
            label="Hetzner CPX11+IPv4+Backups"
            value={`$${data.costs.hetznerUsd.toFixed(2)}`}
          />
          <Stat
            label="Litestream R2 (amortized)"
            value={`$${data.costs.litestreamAmortizedUsd.toFixed(2)}`}
          />
          <Stat
            label="Cloudflare R2"
            value={`$${data.costs.r2Usd.toFixed(4)}`}
          />
          <Stat
            label="Resend"
            value={`$${data.costs.resendUsd.toFixed(2)}`}
          />
          <Stat
            label="Cloudflare DNS/CDN"
            value="$0.00"
          />
        </div>
        <p className="mt-3 text-xs text-muted-foreground">
          What the product itself costs to run. Numbers sourced from the
          canonical _PLATFORM_ITEMS list so the Expenses tab and the P&L tab
          can never drift.
        </p>
      </section>

      <section>
        <SectionHeader>Business-ops costs (last 30 days)</SectionHeader>
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
          <Stat
            label="Stripe fees (live)"
            value={`$${data.businessOps.stripeFeesUsd.toFixed(2)}`}
          />
          <Stat
            label="Taxes"
            value={`$${data.businessOps.taxesUsd.toFixed(2)}`}
          />
          <Stat
            label="Royalties"
            value={`$${data.businessOps.royaltiesUsd.toFixed(2)}`}
          />
          <Stat
            label="Affiliate program"
            value={`$${data.businessOps.affiliateUsd.toFixed(2)}`}
          />
          <Stat
            label="Customer support"
            value={`$${data.businessOps.customerSupportUsd.toFixed(2)}`}
          />
          <Stat
            label="Business-ops total"
            value={`$${data.businessOps.totalUsd.toFixed(2)}`}
          />
        </div>
        <p className="mt-3 text-xs text-muted-foreground">
          Real-world expenses that aren't part of the per-user product
          cost model. Stripe processing + dispute fees pulled live from
          Stripe BalanceTransaction. Tax / royalty / affiliate / support
          slots stay at $0 until each one is set up; they live here so
          the line item exists when the cost activates.
        </p>
        {data.businessOps.errors.length > 0 && (
          <p className="mt-2 text-xs text-yellow-500">
            API issues: {data.businessOps.errors.join(" · ")}
          </p>
        )}
        <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-1 text-xs text-muted-foreground">
          {data.businessOps.notes.stripeFees && (
            <div>
              <span className="font-semibold text-foreground">Stripe fees:</span>{" "}
              {data.businessOps.notes.stripeFees}
            </div>
          )}
          {data.businessOps.notes.taxes && (
            <div>
              <span className="font-semibold text-foreground">Taxes:</span>{" "}
              {data.businessOps.notes.taxes}
            </div>
          )}
          {data.businessOps.notes.royalties && (
            <div>
              <span className="font-semibold text-foreground">Royalties:</span>{" "}
              {data.businessOps.notes.royalties}
            </div>
          )}
          {data.businessOps.notes.affiliate && (
            <div>
              <span className="font-semibold text-foreground">Affiliate:</span>{" "}
              {data.businessOps.notes.affiliate}
            </div>
          )}
          {data.businessOps.notes.customerSupport && (
            <div>
              <span className="font-semibold text-foreground">Customer support:</span>{" "}
              {data.businessOps.notes.customerSupport}
            </div>
          )}
        </div>
      </section>

      <section>
        <SectionHeader>Recurring revenue</SectionHeader>
        <div className="grid grid-cols-2 gap-3">
          <Stat
            label="MRR"
            value={`$${data.revenue.mrrUsd.toFixed(2)}`}
          />
          <Stat
            label="Annualized"
            value={`$${(data.revenue.mrrUsd * 12).toFixed(2)}`}
          />
        </div>
        <p className="mt-3 text-xs text-muted-foreground">
          MRR comes from active membership subscriptions only - it
          excludes one-off storage invoices, which show up in the 30d
          revenue line above instead.
        </p>
      </section>
    </div>
  )
}

// ------------------------------------------------------------------
// Expenses tab - canonical static cost model document. Three buckets
// (Platform / Per-account / Metered) cover every expense vector this
// business is being charged for or could be charged for at known
// thresholds. NO live measurement happens here — see /api/admin/live
// (and the Live tab) for MTD numbers. The data is hardcoded server-
// side and only changes when real-world pricing changes (Hetzner
// re-prices, we cross a free tier, etc.). Sibling document on the
// inflow side: /api/admin/revenue (and the Memberships tab).
// ------------------------------------------------------------------

type MoneyState = "active" | "latent" | "absorbed" | "profitable"

type PlatformItem = {
  name: string
  state: MoneyState
  annualUsd: number
  note: string
}

type MeteredItem = {
  name: string
  state: MoneyState
  ourCost: string
  billedAtMarkup: string
  marginPct: number | null
  note: string
}

type ExpensesData = {
  platform: {
    subtitle: string
    annualActiveUsd: number
    items: PlatformItem[]
  }
  metered: {
    subtitle: string
    markupMultiplier: number
    items: MeteredItem[]
  }
}

function ExpensesTab() {
  const [data, setData] = React.useState<ExpensesData | null>(null)
  const [error, setError] = React.useState<string | null>(null)
  const [loading, setLoading] = React.useState(true)

  // Static structure — fetch once on mount, no polling. The
  // underlying constants only change on deploy.
  React.useEffect(() => {
    let cancelled = false
    const fetchOnce = async () => {
      try {
        const r = await fetch("/api/admin/expenses", { credentials: "include" })
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        const j: ExpensesData = await r.json()
        if (!cancelled) {
          setData(j)
          setError(null)
        }
      } catch (e) {
        if (!cancelled) setError(String(e))
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    fetchOnce()
  }, [])

  if (loading && !data) return <div className="text-sm text-muted-foreground">Loading…</div>
  if (error && !data) return <div className="text-sm text-destructive">Couldn't load: {error}</div>
  if (!data) return null

  return (
    <div className="space-y-8">
      {/* Bucket 1: Static (flat recurring costs) */}
      <MoneyBucket
        title="Static"
        subtitle={data.platform.subtitle}
        accent="platform"
      >
        <MoneyCard
          label="Annual active cost"
          value={`$${data.platform.annualActiveUsd.toFixed(2)}`}
        />
        <MoneyItemTable
          columns={["Service", "State", "Annual", "Note"]}
          rows={data.platform.items.map((i) => [
            i.name,
            <StateBadge key="s" state={i.state} />,
            i.annualUsd === 0 ? "—" : `$${i.annualUsd.toFixed(2)}`,
            i.note,
          ])}
        />
      </MoneyBucket>

      {/* Bucket 2: User Activity (metered per-user) */}
      <MoneyBucket
        title="User Activity"
        accent="metered"
      >
        <MoneyItemTable
          columns={["Service", "State", "Our cost", "Note"]}
          rows={data.metered.items.map((i) => [
            i.name,
            <StateBadge key="s" state={i.state} />,
            i.ourCost,
            i.note,
          ])}
        />
      </MoneyBucket>
    </div>
  )
}

// ------------------------------------------------------------------
// Memberships tab - canonical static document for every tier's
// economics: account-creation fee scenarios + per-GB usage rates +
// billing schedule + add-ons, grouped by tier (Core / Basic / Creator
// / Studio). Was 'Revenue' until the structure shifted from
// flat-revenue-buckets to per-tier-membership-buckets. Inflow-side
// sibling of the Expenses tab. Each bucket also fetches a small
// live-summary stat at request time so the static document is
// anchored to operational reality. For detailed per-invoice revenue
// numbers see the Billing tab; for rolling-30d P&L see the P&L tab.
// Powered by /api/admin/revenue (endpoint name unchanged for now).
// ------------------------------------------------------------------

type RevenueItem = {
  name: string
  state: MoneyState
  // Gross display string for the charge amount (what we bill the user).
  gross: string
  // Net amount per scenario — four "good path" payment methods plus
  // two outcome scenarios (refund, chargeback). All six are shown
  // regardless of likelihood; the table is the complete possibility
  // space for each line item. Strings (not numbers) so items can
  // carry "n/a", "see invoice", or qualified values where per-method
  // net is misleading (rate items, the small-charge surcharge).
  feeNetByMethod: {
    usCard: string
    intlCardUsd: string
    intlCardNonUsd: string
    ach: string
    refund: string
    chargeback: string
  }
  // What Stripe takes per scenario. Parallel to feeNetByMethod —
  // gross = feeAmount + feeNet for payment-method scenarios; for
  // refund/chargeback the amount is the total loss to us. Rendered
  // alongside the formula in the Scenarios sub-table so the reader
  // sees "2.9% + $0.30" and "$0.33" together instead of having to
  // mentally compute.
  feeAmountByMethod: {
    usCard: string
    intlCardUsd: string
    intlCardNonUsd: string
    ach: string
    refund: string
    chargeback: string
  }
  // Optional asterisked qualifier explaining caveats on the fee math
  // (e.g. "per-unit only; fixed fee applies per invoice").
  feeNote: string | null
  // How the charge fires through Stripe — "Stripe Subscription"
  // (auto-renews), "Stripe Invoice" (one-off we generate),
  // "Stripe InvoiceItem" (line added to another invoice).
  mechanism: string
  // When the charge fires (event, cron, anniversary, etc.).
  trigger: string
  // file:function in the backend where the charge originates, so the
  // operator can trace any $ to the code that creates it.
  codeRef: string
  note: string
}

type MembershipLiveSummary = {
  subscriberCount: number | null
  annualGrossUsd: number | null
  annualNetUsd: number | null
  monthlyNetUsd: number | null
}

type UsageLiveSummary = {
  totalGbStored: number | null
  totalGbDownloadedMtd: number | null
  downloadNote?: string
}

type AddOnLiveSummary = {
  surchargesLast30d: number | null
  note?: string
}

type RevenueData = {
  membership: {
    subtitle: string
    items: RevenueItem[]
    liveSummary: MembershipLiveSummary
  }
  usage: {
    subtitle: string
    items: RevenueItem[]
    liveSummary: UsageLiveSummary
  }
  addOns: {
    subtitle: string
    items: RevenueItem[]
    liveSummary: AddOnLiveSummary
  }
}

function MembershipsTab() {
  const [data, setData] = React.useState<RevenueData | null>(null)
  const [error, setError] = React.useState<string | null>(null)
  const [loading, setLoading] = React.useState(true)

  // Static structure + bucket-level live summary stats. Fetched once
  // on mount; live summaries are cheap (single Stripe call + a R2
  // bucket-stats already cached by the bucket-stats helper).
  React.useEffect(() => {
    let cancelled = false
    const fetchOnce = async () => {
      try {
        const r = await fetch("/api/admin/revenue", { credentials: "include" })
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        const j: RevenueData = await r.json()
        if (!cancelled) {
          setData(j)
          setError(null)
        }
      } catch (e) {
        if (!cancelled) setError(String(e))
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    fetchOnce()
  }, [])

  if (loading && !data)
    return <div className="text-sm text-muted-foreground">Loading…</div>
  if (error && !data)
    return <div className="text-sm text-destructive">Couldn't load: {error}</div>
  if (!data) return null

  // Per-item layout: 9 narrow columns for the fee math scenarios
  // (line + state + gross + 4 payment-method nets + 2 outcome nets),
  // then a full-width sub-row per item carrying the wiring info
  // (mechanism, trigger, code) on the left and note + feeNote on
  // the right. Two-row layout keeps the main scannable data
  // narrow enough to fit the viewport while the long-form metadata
  // gets the space it needs without forcing horizontal scroll.

  return (
    <div className="space-y-8">
      {/* Bucket 1: Core Membership (planned ad-supported FREE tier —
          frontend shell only, no backend wiring yet. Sits above Basic
          in the Revenue tab so commercial tiers list top-to-bottom in
          ascending commitment: Core (free) → Basic ($1/yr) → Creator
          ($25/mo) → Studio (enterprise). Revenue here is ad
          impressions, not subscriptions; card labels will change when
          we wire real data.) */}
      <MoneyBucket title="Core Membership" accent="membership">
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3">
          <MoneyCard label="Annual gross revenue" value="—" />
          <MoneyCard label="Active subscribers" value="—" />
          <MoneyCard label="Annual net" value="—" />
          <MoneyCard label="Monthly net" value="—" />
        </div>
      </MoneyBucket>

      {/* Bucket 2: Basic Membership (current MVP tier — was the only
          paid tier under the old "Premium" name; renamed to Basic
          now that Core sits above it). */}
      <MoneyBucket title="Basic Membership" accent="membership">
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3">
          <MoneyCard
            label="Annual gross revenue"
            value={
              data.membership.liveSummary.annualGrossUsd != null
                ? `$${data.membership.liveSummary.annualGrossUsd.toFixed(2)}`
                : "—"
            }
          />
          <MoneyCard
            label="Active subscribers"
            value={
              data.membership.liveSummary.subscriberCount != null
                ? data.membership.liveSummary.subscriberCount.toString()
                : "—"
            }
          />
          <MoneyCard
            label="Annual net"
            value={
              data.membership.liveSummary.annualNetUsd != null
                ? `$${data.membership.liveSummary.annualNetUsd.toFixed(2)}`
                : "—"
            }
          />
          <MoneyCard
            label="Monthly net"
            value={
              data.membership.liveSummary.monthlyNetUsd != null
                ? `$${data.membership.liveSummary.monthlyNetUsd.toFixed(2)}`
                : "—"
            }
          />
        </div>

        {/* Account Creation — the $1/yr Flow fee charged at signup
            (and each anniversary). Scenarios sub-box below shows the
            outcome per payment method + the loss scenarios. */}
        <div className="text-xs uppercase tracking-wider text-muted-foreground font-semibold mt-6 mb-2">
          Account Creation
        </div>
        <RevenueItemTable items={data.membership.items} />

        {/* User Activity — what users do that flows through to our
            infra bill. Three metered line items (storage byte-hours
            + Class A ops + Class B ops), mirrored from the Expenses
            tab so both the platform-cost view and the tier-billing
            view stay in sync. */}
        <div className="text-xs uppercase tracking-wider text-muted-foreground font-semibold mt-6 mb-2">
          User Activity
        </div>
        <BasicUserActivityTable />

        {/* (Unaccounted-costs box was moved to the Expenses tab — all
            items there now live as Platform / Per-account rows, where
            they belong as platform-level cost tracking, not per-user
            billing.) */}

        {/* Billing Schedule — when accumulated usage actually gets
            invoiced. Threshold model + annual rollup is the
            fee-optimization strategy decided 2026-05-23: minimize
            transactions per user per year so the $0.30 fixed-fee
            portion of each Stripe charge bites less. */}
        <div className="text-xs uppercase tracking-wider text-muted-foreground font-semibold mt-6 mb-2">
          Billing Schedule
        </div>
        <BasicBillingScheduleBox />

        {/* Add-ons — surcharges and one-off fees that aren't the
            membership or per-GB usage. Currently just the small-
            charge surcharge that offsets Stripe's fixed-fee bite on
            account-closure final invoices below the minimum. */}
        <div className="text-xs uppercase tracking-wider text-muted-foreground font-semibold mt-6 mb-2">
          Add-ons
        </div>
        <BasicAddOnsTable />
      </MoneyBucket>

      {/* Creator Membership - commercial tier 3 of 4. $25/month
          subscription. Monthly Subscription scenarios + User Activity
          rate table. Not yet wired to backend; data is hardcoded
          until we have a real Creator user. */}
      <MoneyBucket title="Creator Membership" accent="membership">
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3">
          <MoneyCard label="Annual gross revenue" value="—" />
          <MoneyCard label="Active subscribers" value="—" />
          <MoneyCard label="Annual net" value="—" />
          <MoneyCard label="Monthly net" value="—" />
        </div>

        {/* Monthly Subscription - the $25 recurring charge. Same
            scenario shape as Basic's Account Creation, but the
            anniversary cadence is monthly instead of annual. */}
        <div className="text-xs uppercase tracking-wider text-muted-foreground font-semibold mt-6 mb-2">
          Monthly Subscription
        </div>
        <RevenueItemTable items={CREATOR_SUBSCRIPTION_ITEMS} />

        {/* User Activity - storage + ops rates. Per the tier
            architecture memory, Creator gets cheaper bandwidth than
            Basic; exact rates TBD. Same table shape as Basic, with
            TBD pills in the Net column where rates aren't locked. */}
        <div className="text-xs uppercase tracking-wider text-muted-foreground font-semibold mt-6 mb-2">
          User Activity
        </div>
        <CreatorUserActivityTable />
      </MoneyBucket>

      {/* Studio Membership - the high-volume commercial tier. No
          billing-schedule prose here; the subscription + usage rate
          cards are self-explanatory once you have seen Creator's
          shape. Hardcoded frontend until the first real Studio user,
          then move to the server-side _REVENUE_STUDIO_ITEMS path
          like Basic. */}
      <MoneyBucket title="Studio Membership" accent="membership">
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3">
          <MoneyCard label="Annual gross revenue" value="—" />
          <MoneyCard label="Active accounts" value="—" />
          <MoneyCard label="Annual net" value="—" />
          <MoneyCard label="Monthly net" value="—" />
        </div>

        {/* Monthly Subscription - the $2,500 recurring charge. Same
            scenario shape as Creator's, just scaled up. */}
        <div className="text-xs uppercase tracking-wider text-muted-foreground font-semibold mt-6 mb-2">
          Monthly Subscription
        </div>
        <RevenueItemTable items={STUDIO_SUBSCRIPTION_ITEMS} />

        {/* User Activity - bulk storage pricing for enterprise
            volume. R2 ops absorbed at platform level like every
            other tier. */}
        <div className="text-xs uppercase tracking-wider text-muted-foreground font-semibold mt-6 mb-2">
          User Activity
        </div>
        <StudioUserActivityTable />
      </MoneyBucket>

      {/* Internal tiers (Partner / Dev / VIP / Admin) DO NOT belong
          here - they're cost-only account types (they never generate
          revenue). Their per-account infra cost is already implicitly
          covered by the existing Per-account expense lines (Sentry,
          Resend, etc.). The Memberships tab is for tiers that pay us;
          internal tiers will get their own visibility surface (Users
          tab badges / sidebar entries) but not a Memberships bucket. */}

      {/* Standalone Usage bucket removed - it was a leftover from the
          old flat-revenue-buckets design. Storage + bandwidth math
          now lives inside each tier's own User Activity sub-section
          (currently only Basic Membership's) where it belongs. */}

      {/* Standalone Add-ons bucket removed - same leftover-from-old-
          design situation as the Usage bucket. The small-charge
          surcharge already lives inside Basic Membership's own
          Add-ons sub-section where it's tier-scoped. */}

    </div>
  )
}

// Per-item card layout. Each revenue line item is a self-contained
// card with:
//   - Header: Name + State badge + Gross prominently displayed
//   - Two-column body: Wiring (billed via, trigger, code) on the
//     left; Scenarios sub-table (6 rows × 2 columns) on the right
//   - Note + feeNote at the bottom
//
// Scenarios as ROWS (not columns) keeps the layout narrow enough
// to fit any viewport, makes the gross amount unmissable above the
// fold, and gives each scenario room to spell out the full label
// ("Intl card (non-USD)" instead of cramped column headers).
//
// Refund and Chargeback rows render in destructive red since
// they're losses, not earnings.
function RevenueItemTable({ items }: { items: RevenueItem[] }) {
  // When a bucket has only one line item, the bucket title is enough
  // to identify it — skip the per-card header (name + state badge +
  // divider). When there are multiple items, the per-card headers
  // are necessary to tell them apart.
  const showHeader = items.length > 1
  return (
    <div className="space-y-3">
      {items.map((i) => (
        <RevenueItemCard key={i.name} item={i} showHeader={showHeader} />
      ))}
    </div>
  )
}

function RevenueItemCard({
  item: i,
  showHeader,
}: {
  item: RevenueItem
  showHeader: boolean
}) {
  // 6 scenarios in fixed order: 4 payment methods, then 2 outcomes
  // (refund / chargeback) at the bottom in red. Each row's "Stripe
  // takes" cell shows the actual fee AMOUNT for this line item on
  // top + the formula underneath, so the reader sees "$0.33 / 2.9% +
  // $0.30" together without having to mentally compute one from the
  // other.
  const scenarios: Array<{
    label: string
    feeAmount: string
    feeFormula: string
    value: string
    destructive?: boolean
  }> = [
    {
      label: "US card",
      feeAmount: i.feeAmountByMethod.usCard,
      feeFormula: "2.9% + $0.30",
      value: i.feeNetByMethod.usCard,
    },
    {
      label: "International card (USD)",
      feeAmount: i.feeAmountByMethod.intlCardUsd,
      feeFormula: "4.4% + $0.30",
      value: i.feeNetByMethod.intlCardUsd,
    },
    {
      label: "International card (non-USD)",
      feeAmount: i.feeAmountByMethod.intlCardNonUsd,
      feeFormula: "5.4% + $0.30",
      value: i.feeNetByMethod.intlCardNonUsd,
    },
    {
      label: "ACH Direct Debit",
      feeAmount: i.feeAmountByMethod.ach,
      feeFormula: "0.8% (capped at $5)",
      value: i.feeNetByMethod.ach,
    },
    {
      label: "If refunded",
      feeAmount: i.feeAmountByMethod.refund,
      feeFormula: "Stripe keeps the original fee",
      value: i.feeNetByMethod.refund,
      destructive: true,
    },
    {
      label: "If charged back",
      feeAmount: i.feeAmountByMethod.chargeback,
      feeFormula: "Gross reversed + $15 dispute fee",
      value: i.feeNetByMethod.chargeback,
      destructive: true,
    },
  ]

  return (
    <div className="border border-border">
      {/* Header: name + state badge — only rendered when the bucket
          contains multiple items (otherwise the bucket title already
          identifies the line). */}
      {showHeader && (
        <div className="px-4 py-3 border-b border-border bg-muted/10">
          <div className="flex items-baseline gap-3 flex-wrap">
            <h3 className="text-sm font-bold tracking-tight">{i.name}</h3>
            <StateBadge state={i.state} />
          </div>
        </div>
      )}

      {/* Body: full-width scenarios sub-table with three columns —
          scenario label | Stripe's fee for it | resulting net. The
          middle column inlines what used to live in the global
          "Payment Method Fee Schedule" callout so the formula sits
          right next to the resulting net it produces.
          Outer top/bottom padding tuned to compensate for the cells'
          own py-1.5 (header, 6px) and py-2 (body, 8px) — net effect
          is a uniform 16px gap from box edge to nearest text on
          all four sides. */}
      <div className="px-4 pt-2.5 pb-2">
        <table className="w-full text-sm">
          <thead className="text-muted-foreground uppercase tracking-wider text-[10px] font-semibold">
            <tr className="border-b border-border/40">
              <th className="py-1.5 pr-3 text-left font-semibold">
                Scenario
              </th>
              <th className="py-1.5 pr-3 text-left font-semibold">
                Stripe takes
              </th>
              <th className="py-1.5 text-right font-semibold">Net</th>
            </tr>
          </thead>
          <tbody>
            {scenarios.map((s) => (
              <tr
                key={s.label}
                className="border-t border-border/40 align-top"
              >
                <td
                  className={
                    "py-2 pr-3 " + (s.destructive ? "text-destructive" : "")
                  }
                >
                  {s.label}
                </td>
                <td
                  className={
                    "py-1.5 pr-3 whitespace-nowrap " +
                    (s.destructive ? "text-destructive" : "")
                  }
                >
                  <span className="font-mono tabular-nums">{s.feeAmount}</span>
                  <span
                    className={
                      "ml-2 text-xs " +
                      (s.destructive
                        ? "text-destructive/70"
                        : "text-muted-foreground")
                    }
                  >
                    {s.feeFormula}
                  </span>
                </td>
                <td
                  className={
                    "py-2 text-right font-mono tabular-nums whitespace-nowrap " +
                    (s.destructive ? "text-destructive" : "")
                  }
                >
                  {s.value}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Note + feeNote at the bottom. Whole footer block is
          skipped when both fields are empty so we don't render a
          stub padded box. */}
      {(i.note || i.feeNote) && (
        <div className="px-4 py-3 border-t border-border/40 bg-muted/5 text-xs space-y-1">
          {i.note && <div>{i.note}</div>}
          {i.feeNote && (
            <div className="text-muted-foreground italic text-[11px]">
              {i.feeNote}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// Basic-tier user activity — the user-driven actions and resource
// holdings that flow through to our infra bill. Copied 1:1 from the
// Expenses tab's _METERED_ITEMS list (admin.py); three metered line
// items (storage byte-hours + Class A ops + Class B ops). Both the
// Expenses tab and this table
// show the same source-of-truth data; if/when these rates change,
// both update together (eventually via a shared backend payload).
// Bandwidth row intentionally dropped — R2 egress is free + downloads
// are planned to be free for users; charging $0.02/GB was a stale
// assumption.
function BasicUserActivityTable() {
  // Only storage is billed per-user. R2 Class A/B ops used to be
  // billed here too at 2× markup; they're now platform-absorbed
  // (see Expenses tab) since per-user op math wasn't worth the
  // invoice complexity at our scale.
  //
  // Display in GB-month while the internal billing math still
  // integrates at byte-hour precision via StorageObject's
  // uploaded_at/deleted_at timestamps. Net = the user-billed rate minus
  // Backblaze's $0.007/GB-month cost to us. These read from billing.py's
  // constants via /api/billing/prices where possible; the cost side is
  // not exposed there, so it is stated here and must be kept in step
  // with STORAGE_COST_PER_GB_PER_MONTH_USD.
  const rows: Array<{
    service: string
    userPays: string
    net: string
  }> = [
    {
      service: "Backblaze storage (data + metadata)",
      userPays: "2.0¢ / GB-month (Basic)",
      net: "1.3¢ / GB-month",
    },
  ]

  return (
    <div className="border border-border">
      <div className="px-4 pt-2.5 pb-2">
        <table className="w-full text-sm">
          <thead className="text-muted-foreground uppercase tracking-wider text-[10px] font-semibold">
            <tr className="border-b border-border/40">
              <th className="py-1.5 pr-3 text-left font-semibold">Service</th>
              <th className="py-1.5 pr-3 text-left font-semibold">
                User pays
              </th>
              <th className="py-1.5 text-right font-semibold">Net</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr
                key={r.service}
                className="border-t border-border/40 align-top"
              >
                <td className="py-2 pr-3">{r.service}</td>
                <td className="py-2 pr-3 font-mono tabular-nums whitespace-nowrap">
                  {r.userPays}
                </td>
                <td className="py-2 text-right font-mono tabular-nums whitespace-nowrap">
                  {r.net}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// Basic-tier billing schedule — explains the mechanics of how
// accumulated usage gets invoiced (threshold + annual rollup). Plain
// prose rather than a row table since the behavior is a connected
// flow, not a list of independent rules.
function BasicBillingScheduleBox() {
  return (
    <div className="border border-border px-4 py-3 text-sm leading-relaxed space-y-2">
      <p>
        Storage is the only metered line item — R2 ops are platform-
        absorbed (see the Expenses tab&apos;s Per-account bucket) rather
        than billed per user, keeping invoices simple.
      </p>
      <p>
        Storage charges accumulate against a fixed $10 invoice threshold.
        As soon as a user&apos;s accumulated storage usage reaches $10, we issue a
        standalone invoice for that amount and the counter resets to zero.
      </p>
      <p>
        If the user reaches their annual renewal date without hitting $10
        of usage, the accumulated tab folds into the next $1/yr renewal
        charge, so a low-usage account ends up with a single transaction
        per year regardless of how small the usage tab is.
      </p>
      <p className="text-muted-foreground text-xs">
        Fixed rather than configurable at launch, so that billing
        behaviour is the same for every account while the meter is new.
      </p>
    </div>
  )
}

// Basic-tier add-ons — surcharges and one-off fees beyond membership
// + per-GB usage. Currently just the small-charge surcharge that
// offsets Stripe's fixed-fee bite on tiny final invoices when a
// user closes their account below the $5 minimum.
function BasicAddOnsTable() {
  const rows: Array<{
    label: string
    when: string
    amount: string
  }> = [
    {
      label: "Small-charge surcharge",
      when:
        "Final invoice on account closure below the $5 minimum threshold",
      amount: "$0.55",
    },
  ]

  return (
    <div className="border border-border">
      <div className="px-4 pt-2.5 pb-2">
        <table className="w-full text-sm">
          <thead className="text-muted-foreground uppercase tracking-wider text-[10px] font-semibold">
            <tr className="border-b border-border/40">
              <th className="py-1.5 pr-3 text-left font-semibold">
                Surcharge
              </th>
              <th className="py-1.5 pr-3 text-left font-semibold">
                When it applies
              </th>
              <th className="py-1.5 text-right font-semibold">Amount</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr
                key={r.label}
                className="border-t border-border/40 align-top"
              >
                <td className="py-2 pr-3 whitespace-nowrap">{r.label}</td>
                <td className="py-2 pr-3 text-xs text-muted-foreground">
                  {r.when}
                </td>
                <td className="py-2 text-right font-mono tabular-nums whitespace-nowrap">
                  {r.amount}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ---------- Creator Membership tier ($25/month) -----------------
//
// Hardcoded in the frontend until we have a real Creator user and
// can flow it through /api/admin/revenue like Basic does. When that
// happens, move the items array into _REVENUE_CREATOR_ITEMS server-
// side and read from data.creator.items.

const CREATOR_SUBSCRIPTION_ITEMS: RevenueItem[] = [
  {
    name: "Monthly subscription",
    state: "latent",
    gross: "$25.00 / mo",
    // $25 × {2.9% + $0.30, 4.4% + $0.30, 5.4% + $0.30, 0.8% (uncapped)}.
    // Refund/chargeback math mirrors backend billing_lib:
    //   refund = -(gross × 2.9% + $0.30)  [Stripe keeps original US-card fee]
    //   chargeback = -(gross + $15 dispute fee)
    feeAmountByMethod: {
      usCard: "$1.03",
      intlCardUsd: "$1.40",
      intlCardNonUsd: "$1.65",
      ach: "$0.20",
      refund: "$1.03",
      chargeback: "$40.00",
    },
    feeNetByMethod: {
      usCard: "$23.98",
      intlCardUsd: "$23.60",
      intlCardNonUsd: "$23.35",
      ach: "$24.80",
      refund: "-$1.03",
      chargeback: "-$40.00",
    },
    feeNote: null,
    mechanism: "Stripe Subscription",
    trigger: "Monthly on subscription anniversary",
    codeRef: "TBD — not wired yet",
    note: "",
  },
]

// Creator user activity rates - storage $0.01/GB-month. Ops are
// platform-absorbed (B2 transactions are free).
function CreatorUserActivityTable() {
  const rows: Array<{
    service: string
    userPays: string
    net: string
  }> = [
    {
      service: "B2 Storage (data + metadata)",
      userPays: "1¢ / GB-month",
      net: "0.3¢ / GB-month",
    },
  ]

  return (
    <div className="border border-border">
      <div className="px-4 pt-2.5 pb-2">
        <table className="w-full text-sm">
          <thead className="text-muted-foreground uppercase tracking-wider text-[10px] font-semibold">
            <tr className="border-b border-border/40">
              <th className="py-1.5 pr-3 text-left font-semibold">Service</th>
              <th className="py-1.5 pr-3 text-left font-semibold">
                User pays
              </th>
              <th className="py-1.5 text-right font-semibold">Net</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr
                key={r.service}
                className="border-t border-border/40 align-top"
              >
                <td className="py-2 pr-3">{r.service}</td>
                <td className="py-2 pr-3 font-mono tabular-nums whitespace-nowrap">
                  {r.userPays}
                </td>
                <td className="py-2 text-right font-mono tabular-nums whitespace-nowrap">
                  {r.net}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ---------- Studio Membership tier ----------------
//
// The high-volume commercial tier. Same hardcoded-frontend shape as
// Creator; move into _REVENUE_STUDIO_ITEMS server-side once a real
// Studio account flows through /api/admin/revenue.

const STUDIO_SUBSCRIPTION_ITEMS: RevenueItem[] = [
  {
    name: "Monthly subscription",
    state: "latent",
    gross: "$2,500.00 / mo",
    // $2,500 × {2.9% + $0.30, 4.4% + $0.30, 5.4% + $0.30, 0.8% (capped at $5)}.
    // Refund/chargeback math mirrors backend billing_lib:
    //   refund     = -(gross × 2.9% + $0.30)  [Stripe keeps original US-card fee]
    //   chargeback = -(gross + $15 dispute fee)
    feeAmountByMethod: {
      usCard: "$72.80",
      intlCardUsd: "$110.30",
      intlCardNonUsd: "$135.30",
      ach: "$5.00",
      refund: "$72.80",
      chargeback: "$2,515.00",
    },
    feeNetByMethod: {
      usCard: "$2,427.20",
      intlCardUsd: "$2,389.70",
      intlCardNonUsd: "$2,364.70",
      ach: "$2,495.00",
      refund: "-$72.80",
      chargeback: "-$2,515.00",
    },
    feeNote: null,
    mechanism: "Stripe Subscription",
    trigger: "Monthly on subscription anniversary",
    codeRef: "TBD — not wired yet",
    note: "",
  },
]

// Studio user activity rates - storage $0.0075/GB-month. Ops are
// platform-absorbed (B2 transactions are free).
function StudioUserActivityTable() {
  const rows: Array<{
    service: string
    userPays: string
    net: string
  }> = [
    {
      service: "B2 Storage (data + metadata)",
      userPays: "0.75¢ / GB-month",
      net: "0.05¢ / GB-month",
    },
  ]

  return (
    <div className="border border-border">
      <div className="px-4 pt-2.5 pb-2">
        <table className="w-full text-sm">
          <thead className="text-muted-foreground uppercase tracking-wider text-[10px] font-semibold">
            <tr className="border-b border-border/40">
              <th className="py-1.5 pr-3 text-left font-semibold">Service</th>
              <th className="py-1.5 pr-3 text-left font-semibold">
                User pays
              </th>
              <th className="py-1.5 text-right font-semibold">Net</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr
                key={r.service}
                className="border-t border-border/40 align-top"
              >
                <td className="py-2 pr-3">{r.service}</td>
                <td className="py-2 pr-3 font-mono tabular-nums whitespace-nowrap">
                  {r.userPays}
                </td>
                <td className="py-2 text-right font-mono tabular-nums whitespace-nowrap">
                  {r.net}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ------------------------------------------------------------------
// Live tab - moment-to-moment cost telemetry. Temporary single home
// for everything that used to live in the Expenses tab Metered
// section plus the platform subscriber/coverage math. Eventually
// disperses to P&L (revenue × cost rollup), Stack (per-service
// boxes), and Users (per-user metered breakdown). Polls every 30s.
// Powered by /api/admin/live.
// ------------------------------------------------------------------

type LiveData = {
  platform: {
    annualCostUsd: number
    subscriberCount: number
    currentAnnualFeeUsd: number
    coverageUsd: number
    uncoveredUsd: number
    breakEvenSubscribers: number
    recommendedFeeUsd: number | null
  }
  metered: {
    monthToDateCostUsd: number
    monthToDateBilledUsd: number
    monthToDateMarginUsd: number
    marginPct: number | null
    markupMultiplier: number
    services: {
      name: string
      costUsd: number
      billedUsd: number
      marginUsd: number
      detail: string
    }[]
  }
  asOf: string
}

function LiveTab() {
  const [data, setData] = React.useState<LiveData | null>(null)
  const [error, setError] = React.useState<string | null>(null)
  const [loading, setLoading] = React.useState(true)

  React.useEffect(() => {
    let cancelled = false
    const fetchOnce = async () => {
      try {
        const r = await fetch("/api/admin/live", { credentials: "include" })
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        const j: LiveData = await r.json()
        if (!cancelled) {
          setData(j)
          setError(null)
        }
      } catch (e) {
        if (!cancelled) setError(String(e))
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    fetchOnce()
    const id = setInterval(fetchOnce, 30_000)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  if (loading && !data) return <div className="text-sm text-muted-foreground">Loading…</div>
  if (error && !data) return <div className="text-sm text-destructive">Couldn't load: {error}</div>
  if (!data) return null

  return (
    <div className="space-y-8">
      {/* Header explaining the temporary nature */}
      <div className="border border-border p-4 bg-muted/10">
        <h2 className="text-sm font-semibold tracking-tight">
          Live cost telemetry — moment-to-moment numbers
        </h2>
        <p className="text-xs text-muted-foreground mt-1.5 max-w-3xl">
          Subscriber/coverage math compared against the Expenses tab's static platform
          total · MTD metered numbers (cost-to-us, billed-to-users, gross margin) ·
          updates every 30 seconds. Temporary home — will eventually fold into the
          P&L tab (revenue × cost rollup), Stack tab boxes (per-service telemetry),
          and Users tab (per-user metered breakdown).
        </p>
      </div>

      {/* Platform live: subscriber/coverage math */}
      <MoneyBucket
        title="Platform coverage"
        subtitle={`How well today's $${data.platform.currentAnnualFeeUsd.toFixed(2)}/yr membership fee covers the platform overhead from the Expenses tab.`}
        accent="platform"
      >
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <MoneyCard
            label="Platform overhead"
            value={`$${data.platform.annualCostUsd.toFixed(2)}/yr`}
            note="from Expenses tab static total"
          />
          <MoneyCard
            label="Subscribers"
            value={data.platform.subscriberCount.toString()}
            note={`at $${data.platform.currentAnnualFeeUsd.toFixed(2)}/yr`}
          />
          <MoneyCard
            label="Coverage"
            value={`$${data.platform.coverageUsd.toFixed(2)}`}
            note={
              data.platform.uncoveredUsd > 0
                ? `$${data.platform.uncoveredUsd.toFixed(2)} short`
                : "fully covered"
            }
            warn={data.platform.uncoveredUsd > 0}
          />
          <MoneyCard
            label="Break-even"
            value={`${data.platform.breakEvenSubscribers} subs`}
            note={
              data.platform.recommendedFeeUsd !== null
                ? `or $${data.platform.recommendedFeeUsd.toFixed(2)}/yr at current count`
                : undefined
            }
          />
        </div>
      </MoneyBucket>

      {/* Metered live: MTD numbers */}
      <MoneyBucket
        title="Metered MTD"
        subtitle={`Live month-to-date numbers from the per-user ledger (StorageObject + R2OperationLog, excluding __platform__ subject). Margin should hover near 50% (the locked ${data.metered.markupMultiplier}× markup).`}
        accent="metered"
      >
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <MoneyCard
            label="MTD cost to us"
            value={`$${data.metered.monthToDateCostUsd.toFixed(4)}`}
          />
          <MoneyCard
            label="MTD billed to users"
            value={`$${data.metered.monthToDateBilledUsd.toFixed(4)}`}
          />
          <MoneyCard
            label="MTD margin"
            value={`$${data.metered.monthToDateMarginUsd.toFixed(4)}`}
            note={
              data.metered.marginPct !== null
                ? `${data.metered.marginPct.toFixed(1)}%`
                : undefined
            }
          />
          <MoneyCard
            label="Markup"
            value={`${data.metered.markupMultiplier}×`}
            note="derived from the tier price map"
          />
        </div>
        <MoneyServiceTable
          columns={["Service", "Detail", "Cost", "Billed", "Margin"]}
          rows={data.metered.services.map((s) => [
            s.name,
            s.detail,
            `$${s.costUsd.toFixed(4)}`,
            `$${s.billedUsd.toFixed(4)}`,
            `$${s.marginUsd.toFixed(4)}`,
          ])}
        />
      </MoneyBucket>

      <div className="text-[11px] text-muted-foreground">
        As of {new Date(data.asOf).toLocaleString()}. Updates every 30 seconds.
        Source: /api/admin/live.
      </div>
    </div>
  )
}

// Small visual badge for the Active/Latent lifecycle state. Solid
// for active (we're paying); outline+amber for latent (waiting to
// activate at a threshold).
function StateBadge({ state }: { state: MoneyState }) {
  // Active is the default state — render nothing so the badge only
  // appears as a signal on items that deviate from "boring active
  // cost line." Latent (amber) = in-architecture but requires a
  // manual activation step. Absorbed (red) = real cost we eat at
  // the platform layer instead of billing users for — red because
  // it's a direct hit to platform margin. Profitable (emerald) =
  // metered cost we bill users at markup (revenue line).
  if (state === "active") return null
  if (state === "profitable") {
    return (
      <span className="inline-block text-[9px] uppercase tracking-wider font-bold px-1.5 py-0.5 bg-emerald-500/10 text-emerald-500 border border-emerald-500/40">
        profitable
      </span>
    )
  }
  if (state === "absorbed") {
    return (
      <span className="inline-block text-[9px] uppercase tracking-wider font-bold px-1.5 py-0.5 bg-red-500/10 text-red-500 border border-red-500/40">
        absorbed
      </span>
    )
  }
  return (
    <span className="inline-block text-[9px] uppercase tracking-wider font-bold px-1.5 py-0.5 bg-amber-500/10 text-amber-500 border border-amber-500/40">
      latent
    </span>
  )
}

// Variant of MoneyServiceTable that accepts JSX cells (for badges,
// multi-line notes, etc.) and aligns the first column left, rest left
// too (since these tables include note columns that should flow).
function MoneyItemTable({
  columns,
  rows,
}: {
  columns: string[]
  rows: React.ReactNode[][]
}) {
  return (
    <div className="border border-border">
      <table className="w-full text-sm">
        <thead className="bg-muted/30 text-[10px] uppercase tracking-wider text-muted-foreground">
          <tr>
            {columns.map((c) => (
              <th key={c} className="px-3 py-2 text-left font-semibold align-top">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className="border-t border-border align-top">
              {row.map((cell, j) => (
                <td key={j} className="px-3 py-2 text-xs">
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function MoneyBucket({
  title,
  subtitle,
  accent: _accent,
  children,
}: {
  title: string
  subtitle?: string
  // Accent prop kept in the signature so callers don't need to
  // change — but the colored dot it used to render was decorative
  // (and the cross-tab color pairing it encoded was invisible
  // without a legend). Render plain headers instead.
  accent:
    | "platform"
    | "per-account"
    | "metered"
    | "membership"
    | "usage"
    | "add-ons"
  children: React.ReactNode
}) {
  return (
    <section className="border border-border">
      <header className="border-b border-border px-4 py-3 bg-muted/20">
        <h2 className="text-base font-bold tracking-tight">{title}</h2>
        {subtitle && (
          <p className="text-xs text-muted-foreground mt-1.5 max-w-3xl">
            {subtitle}
          </p>
        )}
      </header>
      <div className="p-4 space-y-4">{children}</div>
    </section>
  )
}

function MoneyCard({
  label,
  value,
  note,
  warn,
}: {
  label: string
  value: string
  note?: string
  warn?: boolean
}) {
  return (
    <div
      className={`border ${warn ? "border-amber-500/60" : "border-border"} p-3`}
    >
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className="mt-1 font-mono text-lg font-bold tabular-nums">
        {value}
      </div>
      {note && (
        <div
          className={`mt-0.5 text-[11px] ${warn ? "text-amber-500" : "text-muted-foreground"}`}
        >
          {note}
        </div>
      )}
    </div>
  )
}

function MoneyServiceTable({
  columns,
  rows,
}: {
  columns: string[]
  rows: (string | number)[][]
}) {
  return (
    <div className="border border-border">
      <table className="w-full text-sm">
        <thead className="bg-muted/30 text-[10px] uppercase tracking-wider text-muted-foreground">
          <tr>
            {columns.map((c, i) => (
              <th
                key={c}
                className={`px-3 py-2 ${i === 0 ? "text-left" : "text-right"} font-semibold`}
              >
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className="border-t border-border">
              {row.map((cell, j) => (
                <td
                  key={j}
                  className={`px-3 py-2 ${j === 0 ? "text-left" : "text-right font-mono tabular-nums text-xs"}`}
                >
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ------------------------------------------------------------------
// Stack tab - pulls /STACK.md from the server and renders it. We keep
// the renderer minimal (headings, lists, links, inline code, tables)
// since this is an admin-only page and the source is trusted markdown
// we wrote ourselves, not user-supplied content.
// ------------------------------------------------------------------

function StackTab() {
  const [md, setMd] = React.useState<string | null>(null)
  const [error, setError] = React.useState<string | null>(null)

  React.useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const r = await fetch("/api/admin/stack", { credentials: "include" })
        if (!r.ok) {
          setError(`HTTP ${r.status}`)
          return
        }
        const json = (await r.json()) as { markdown: string }
        if (!cancelled) setMd(json.markdown)
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed")
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  if (error)
    return (
      <div className="text-sm text-destructive">Couldn't load: {error}</div>
    )
  if (md === null)
    return <div className="text-sm text-muted-foreground">Loading…</div>

  // The top of STACK.md (title + intro + at-a-glance + the Recurring
  // Subscriptions section, which now lives in our AccountBoxes) is
  // replaced by the boxes below. The rest of the page (R2, Stripe,
  // free tier, etc.) is still rendered from markdown until each one
  // gets its own box.
  const choppedMd = chopMarkdownAtBoxedSections(md)

  return (
    <div className="space-y-8">
      <HetznerAccountBox />
      <ResendAccountBox />
      <SentryAccountBox />
      <GoogleCloudAccountBox />
      <GitHubAccountBox />
      <CloudflareAccountBox />
      <BackblazeAccountBox />
      <StripeAccountBox />
      <MercuryAccountBox />
      <Markdown source={choppedMd} />
    </div>
  )
}

// During the audit pass we deliberately KEEP the old markdown
// sections rendered below the AccountBoxes so the operator can
// cross-reference box vs source to verify nothing was missed.
// As each subscription gets boxed + signed off, the corresponding
// markdown section is REMOVED FROM STACK.md (not chopped here) so
// the doc and the UI stay consistent. Hetzner / Stripe
// are boxed and their STACK.md sections are now pointers to the
// AccountBox + audit doc. R2 still has a markdown section since
// there's no standalone R2AccountBox (R2 ops live as a sub-section
// in CloudflareAccountBox). When R2 gets its own box, drop the
// R2 STACK.md section too — the needle below should rarely need
// to move.
function chopMarkdownAtBoxedSections(src: string): string {
  const NEEDLE = "## Variable costs"
  const idx = src.indexOf(NEEDLE)
  return idx >= 0 ? src.slice(idx) : src
}

// Service-health response shape. Backend at /api/admin/service-health.
type ServiceHealth = {
  status: "active" | "warning" | "down"
  diskFreeGb?: number | null
  diskUsedPct?: number | null
  memUsedPct?: number | null
  uptimeSeconds?: number | null
  // Manual snapshot count via Hetzner Cloud API. Null when HCLOUD_TOKEN
  // isn't configured (UI shows "—"); 0 is the expected steady state;
  // any positive value is a warning we never intended to create one.
  manualSnapshots?: number | null
  // Other Hetzner resources we don't expect to exist. Same UI pattern
  // as manualSnapshots — null when unconfigured, 0 is clean, >0
  // surfaces as yellow warning since each one accrues real cost.
  volumes?: number | null
  floatingIps?: number | null
  loadBalancers?: number | null
  // Outbound bandwidth this billing cycle vs included cap, both in
  // bytes. Null when HCLOUD_TOKEN isn't configured. Warning at >=50%
  // of cap so we have runway before any overage hits.
  bandwidth?: { outgoingBytes: number; includedBytes: number } | null
  warnings?: string[]
}

type ResendHealth = {
  status: "active" | "warning" | "down"
  sendsToday: number
  sendsThisMonth: number
  dailyCap: number
  monthlyCap: number
  warnings?: string[]
}

type SentryHealth = {
  status: "active" | "warning" | "down"
  // Null when SENTRY_AUTH_TOKEN isn't configured; integer count when
  // the live API call succeeded. Frontend shows "—" for null.
  eventsThisMonth: number | null
  monthlyCap: number
  warnings?: string[]
}

// Cloudflare health. Null fields when CLOUDFLARE_API_TOKEN isn't
// configured or the GraphQL/REST call failed — UI shows "—".
type CloudflareBandwidth = {
  bytes: number
  cachedBytes: number
  requests: number
  cachedRequests: number
  threats: number
}
// R2 ops summary from our own R2OperationLog (DB-only, never hits CF).
// Per-bucket so we can separate user-driven from Litestream-driven ops.
type R2OpsBucketCounts = { classA: number; classB: number }
type R2OpsSummary = {
  last24h: Record<string, R2OpsBucketCounts>
  monthToDate: Record<string, R2OpsBucketCounts>
  userContentBucket: string
  backupsBucket: string
}
// Phase E-reconcile: per-bucket comparison of our ledger vs Cloudflare's
// authoritative r2OperationsAdaptiveGroups counts, plus drift percentages.
type R2OpsBucketCompare = {
  ledgerA: number
  ledgerB: number
  cfA: number
  cfB: number
  cfFree: number
  cfUnknown: number
  driftAPct: number
  driftBPct: number
}
type R2OpsReconciliation = {
  last24h: Record<string, R2OpsBucketCompare>
  monthToDate: Record<string, R2OpsBucketCompare>
  warnings: string[]
  userContentBucket: string
}
type CloudflareHealth = {
  status: "active" | "warning" | "down"
  configured: boolean
  warnings?: string[]
  dnsRecordCount?: number | null
  dnsRecordsCap?: number
  emailRoutingRuleCount?: number | null
  emailRoutingRulesCap?: number
  last24h?: CloudflareBandwidth | null
  last3d?: CloudflareBandwidth | null
  r2Ops?: R2OpsSummary
  r2OpsReconciliation?: R2OpsReconciliation | null
  r2OpsClassAFreeTier?: number
  r2OpsClassBFreeTier?: number
  r2BillingSummary?: BillReconciliationData | null
  // Point-in-time bytes-on-disk per bucket × storage-class, from
  // CF's GraphQL analytics. Keyed `{bucket: {sc: {payloadSize,
  // metadataSize, objectCount}}}`. Null when the account-analytics
  // token isn't configured.
  r2StorageSnapshot?: Record<
    string,
    Record<
      string,
      { payloadSize: number; metadataSize: number; objectCount: number }
    >
  > | null
}

type BackblazeHealth = {
  status?: "active" | "warning" | "down"
  configured?: boolean
  bucket?: string
  region?: string | null
  endpoint?: string | null
  storageBytes?: number | null
  objectCount?: number | null
}

type ServiceHealthMap = {
  hetzner?: ServiceHealth
  resend?: ResendHealth
  sentry?: SentryHealth
  cloudflare?: CloudflareHealth
  backblaze?: BackblazeHealth
}

// Poll the service-health endpoint every 30s. Returns "down" if the
// endpoint itself fails (since by definition something's wrong).
function useServiceHealth(): ServiceHealthMap {
  const [data, setData] = React.useState<ServiceHealthMap>({})
  React.useEffect(() => {
    let cancelled = false
    const fetchOnce = async () => {
      try {
        const r = await fetch("/api/admin/service-health", {
          credentials: "include",
        })
        if (!r.ok) {
          if (!cancelled) setData({ hetzner: { status: "down" } })
          return
        }
        const json = (await r.json()) as ServiceHealthMap
        if (!cancelled) setData(json)
      } catch {
        if (!cancelled) setData({ hetzner: { status: "down" } })
      }
    }
    void fetchOnce()
    const id = window.setInterval(fetchOnce, 30_000)
    return () => {
      cancelled = true
      window.clearInterval(id)
    }
  }, [])
  return data
}

// ------------------------------------------------------------------
// AccountBox — one structured panel per external service. Hetzner
// was the prototype. Boxed so far: Hetzner, Resend, Sentry,
// Google Cloud, GitHub. Still pending: Cloudflare, R2, Stripe.
// ------------------------------------------------------------------

function GoogleCloudAccountBox() {
  const { ownerEmail } = useAdminIdentifiers()
  return (
    <AccountBox
      name="Google Cloud"
      consoleUrl="https://console.cloud.google.com"
      consoleLabel="Console"
      accountUrl="https://console.cloud.google.com/billing"
      accountLabel="Billing"
      status="active"
      keyMetrics={[
        { label: "Project", value: "aether-archive-tool" },
        { label: "API quota", value: "10,000 units/day" },
        { label: "2FA", value: "Enabled" },
      ]}
      notes={[
        "YouTube quota exhaustion is silent: when daily 10k units run out, ALL API calls return 403 until midnight Pacific. Channel imports + metadata rescans fail until then. Quota is per project, shared across all our users. At ~20 units per channel import we'd need ~500 imports in a single day to hit it — comfortable for MVP scale. If we ever need more, we file a quota increase request (free, takes weeks of justification).",
        "Surprise-bill risk isn't quota — it's accidentally enabling a paid GCP service. The console has lots of 'Try Cloud Run / BigQuery / Storage' buttons that flip billing on. Mitigated by the 'Free tier tripwire' budget alert configured across all projects + all services on the ARCHIVE336 billing account — Google emails the moment we approach actual spend. Threshold sized to catch real accidental enablement without false positives from sub-penny rounding noise.",
        "If the OAuth credentials ever leak (client ID is public-ish in OAuth flows but client secret is sensitive), rotate via Console → APIs & Services → Credentials → edit OAuth client → Reset Secret. Then update GOOGLE_CLIENT_SECRET in /opt/aether/.env and restart archive336-api.",
      ]}
      details={[
        {
          label: "Login",
          value: ownerEmail,
          mono: true,
        },
        {
          label: "Credentials",
          value: "GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET in /opt/aether/.env",
          mono: true,
        },
        {
          label: "OAuth redirect URI",
          value: "https://archive336.com/api/auth/youtube/callback",
          mono: true,
        },
        {
          label: "Scopes",
          value:
            "openid · email · profile · youtube.readonly · youtube.force-ssl",
        },
      ]}
    />
  )
}

function GitHubAccountBox() {
  return (
    <AccountBox
      name="GitHub"
      consoleUrl="https://github.com/afraaz-llc/archive336"
      consoleLabel="Repo"
      accountUrl="https://github.com/settings/billing"
      accountLabel="Account"
      status="active"
      keyMetrics={[
        { label: "Yearly cost", value: "$0" },
        { label: "Disk usage", value: "1.3 MB / 5 GB" },
        { label: "2FA", value: "Enabled" },
      ]}
      details={[
        {
          label: "Clone (SSH)",
          value: "git@github.com:afraaz-llc/archive336.git",
          mono: true,
        },
        {
          label: "Default branch",
          value: "main",
          mono: true,
        },
        {
          label: "Visibility",
          value: "Private",
        },
        {
          label: "License",
          value: "None (proprietary)",
        },
      ]}
    />
  )
}

// Account ID and zone tag pinned here for the URL constructions. These
// only change if we ever migrate accounts, which would be a much
// larger surgery than swapping a constant. Worth hardcoding so the
// box renders correctly even when the API call is down.
// Account ids and the origin IP come from the server, admin-gated.
//
// They used to be constants right here. This file is lazy-loaded, which
// is not access control: the chunk is a static asset that returns 200
// to anyone who asks for it, so the origin IP and the Mercury account
// id were being served to every visitor of the site. The origin IP was
// the real cost - the site sits behind Cloudflare's proxy, which only
// protects an origin nobody can address directly.
type AdminIdentifiers = {
  originIpv4: string
  originIpv6: string
  sshKeyPath: string
  ownerEmail: string
  providerLogin: string
  cloudflareLogin: string
  mercuryAccountId: string
  stripeAccountId: string
  cloudflareAccountId: string
}

const EMPTY_IDENTIFIERS: AdminIdentifiers = {
  originIpv4: "",
  originIpv6: "",
  sshKeyPath: "",
  ownerEmail: "",
  providerLogin: "",
  cloudflareLogin: "",
  mercuryAccountId: "",
  stripeAccountId: "",
  cloudflareAccountId: "",
}

function useAdminIdentifiers(): AdminIdentifiers {
  const [ids, setIds] = React.useState<AdminIdentifiers>(EMPTY_IDENTIFIERS)
  React.useEffect(() => {
    let cancelled = false
    fetch("/api/admin/identifiers", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (!cancelled && d) setIds(d as AdminIdentifiers)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [])
  return ids
}
const CLOUDFLARE_ZONE_TAG = "bc1045d30203c9d4fbef6e448dcb6adc"

function BackblazeAccountBox() {
  const { providerLogin } = useAdminIdentifiers()
  const health = useServiceHealth()
  const bb = health.backblaze
  const status = bb?.status ?? "active"

  const storageCard =
    bb?.storageBytes != null ? formatBytes(bb.storageBytes) : "—"
  const objectsCard =
    bb?.objectCount != null ? bb.objectCount.toLocaleString() : "—"
  // Free-tier headroom: B2's first 10 GB is free, so this is the "when do we
  // start paying for storage" watch (decimal GB to match B2's billing).
  const freeTierCard =
    bb?.storageBytes != null
      ? `${(bb.storageBytes / 1_000_000_000).toFixed(1)} / 10 GB`
      : "—"

  return (
    <AccountBox
      name="Backblaze"
      consoleUrl="https://secure.backblaze.com/b2_buckets.htm"
      consoleLabel="Buckets"
      accountUrl="https://secure.backblaze.com/account_settings.htm"
      accountLabel="Account"
      status={status}
      keyMetrics={[
        { label: "Storage", value: storageCard },
        { label: "Objects", value: objectsCard },
        { label: "Free tier", value: freeTierCard },
      ]}
      notes={[
        "Video + thumbnail storage, migrated off Cloudflare R2 on 2026-06-03. Pay-as-you-go $6.95/TB-mo, first 10 GB free. Class A/B/C transactions free; only the rare Class D costs. No spend cap - the guard is a private bucket + a scoped app key (read/write on this bucket only).",
        "Egress is free to Cloudflare via the Bandwidth Alliance, so downloads (proxied through the dl.archive336.com Worker) cost nothing. Direct B2 egress is free up to 3x stored bytes/mo then $0.01/GB - we stay under that by routing through the Worker.",
      ]}
      details={[
        { section: "Identity" },
        { label: "Workspace", value: "Afraaz LLC" },
        {
          label: "Login",
          value: providerLogin,
          mono: true,
        },
        { label: "2FA", value: "Not enabled" },
        { section: "Setup" },
        {
          label: "Bucket",
          value: bb?.bucket ?? "aether-archive-prod",
          mono: true,
        },
        { label: "Region", value: bb?.region ?? "us-east-005", mono: true },
        {
          label: "Endpoint",
          value: bb?.endpoint ?? "s3.us-east-005.backblazeb2.com",
          mono: true,
        },
        {
          label: "App key",
          value:
            "STORAGE_ACCESS_KEY_ID / STORAGE_SECRET_ACCESS_KEY in /opt/aether/.env (mode 600)",
          mono: true,
        },
        {
          label: "Access",
          value: "Private bucket · scoped app key (read+write, this bucket only)",
        },
        {
          label: "Egress",
          value: "Free via the dl.archive336.com Worker + Bandwidth Alliance",
        },
      ]}
    />
  )
}


function CloudflareAccountBox() {
  const {
    cloudflareAccountId: CLOUDFLARE_ACCOUNT_ID,
    ownerEmail,
    cloudflareLogin,
  } = useAdminIdentifiers()
  const health = useServiceHealth()
  const cf = health.cloudflare
  const status = cf?.status ?? "active"

  // Live cap-approach metrics — turn yellow at 75% via the warnings
  // string the backend computes. We just display the raw count.
  const dnsCard = renderCapMetric(
    cf?.dnsRecordCount,
    cf?.dnsRecordsCap ?? 200,
  )
  const emailCard = renderCapMetric(
    cf?.emailRoutingRuleCount,
    cf?.emailRoutingRulesCap ?? 200,
  )
  // Bandwidth + cache hit ratio from the last 24h. Cache hit ratio is
  // most useful as a leading indicator for Hetzner egress pressure
  // (cache miss = origin hit = Hetzner bandwidth bill, not Cloudflare).
  const day = cf?.last24h
  const bandwidthCard = day
    ? formatBytes(day.bytes)
    : "—"
  const hitRatioCard = day && day.requests > 0
    ? `${((day.cachedRequests / day.requests) * 100).toFixed(1)}%`
    : day && day.bytes > 0
      ? `${((day.cachedBytes / day.bytes) * 100).toFixed(1)}% (bytes)`
      : "—"

  // R2 ops free-tier headroom — still useful as at-a-glance cards
  // for "are we about to bust through the free tier?" The per-bucket
  // ledger-vs-CF breakdown moved into the Bill Reconciliation panel
  // below where it's much more readable.
  const r2 = cf?.r2Ops
  const monthA = r2
    ? Object.values(r2.monthToDate).reduce((s, b) => s + b.classA, 0)
    : null
  const monthB = r2
    ? Object.values(r2.monthToDate).reduce((s, b) => s + b.classB, 0)
    : null
  const classAFreeTier = cf?.r2OpsClassAFreeTier ?? 1_000_000
  const classBFreeTier = cf?.r2OpsClassBFreeTier ?? 10_000_000
  const opsACard = r2
    ? `${(monthA ?? 0).toLocaleString()} / ${(classAFreeTier / 1_000_000).toFixed(0)}M`
    : "—"
  const opsBCard = r2
    ? `${(monthB ?? 0).toLocaleString()} / ${(classBFreeTier / 1_000_000).toFixed(0)}M`
    : "—"
  // (B2 storage moved to its own Backblaze box; cf.r2StorageSnapshot still
  // carries it but it's rendered there now, not here.)

  return (
    <AccountBox
      name="Cloudflare"
      consoleUrl={`https://dash.cloudflare.com/${CLOUDFLARE_ACCOUNT_ID}/archive336.com/`}
      consoleLabel="Zone"
      accountUrl={`https://dash.cloudflare.com/${CLOUDFLARE_ACCOUNT_ID}/`}
      accountLabel="Account"
      status={status}
      statusDetail={cf?.warnings?.[0]}
      keyMetrics={[
        { label: "R2 ops A · mo", value: opsACard },
        { label: "R2 ops B · mo", value: opsBCard },
        { label: "DNS records", value: dnsCard },
        { label: "Email rules", value: emailCard },
        { label: "Bandwidth 24h", value: bandwidthCard },
        { label: "Cache hit 24h", value: hitRatioCard },
      ]}
      notes={[
        "**Surprise-bill paths on the Free plan (all require a manual dashboard click — no Worker, app, or default change has ever flipped Free→Paid):** Argo Smart Routing (1-click opt-in, big-bill horror cases documented), Containers (phantom-bill case), Cache Reserve (Cache Rules UI enable), R2 Infrequent Access (no free tier on ops), Stream (sandbox sub instant conversion), Image Transformations (URL param abuse), Workers Logs (Apr 2025+), Durable Objects SQLite (Jan 7 2026+). **Defense: never click any of those tiles.** Full list with sources in docs/CLOUDFLARE_AUDIT.md §4.",
        "Watch items where billing is 'coming': Workers Analytics Engine and R2 SQL are currently free + billing-deferred. Neither is relevant unless we add Workers or R2 SQL queries — we don't.",
        "Cloudflare has NO spend cap on usage-based services. Budget alerts are email-only and don't pause anything. Our cap enforcement = not enabling those products in the first place. Monthly audit of dash.cloudflare.com/.../billing/subscriptions catches anything that snuck in.",
        "Nothing on the free tier generates per-user cost. A user hammering an uncacheable API endpoint amplifies Hetzner egress, not Cloudflare's bill — that's why TODO #6 (rate-limit /export) and #7 (stream + paginate) matter in the Hetzner picture, not here.",
        "Free-plan analytics caps: httpRequests1hGroups dataset has a 3-day max time-range, which is why we pull last24h here; a 'this month' rollup would need Pro plan or origin-side instrumentation — both deferred.",
      ]}
      details={[
        { section: "Identity" },
        {
          label: "Login",
          value: cloudflareLogin,
          mono: true,
        },
        {
          label: "Account",
          value: CLOUDFLARE_ACCOUNT_ID,
          mono: true,
        },
        {
          label: "Zone",
          value: CLOUDFLARE_ZONE_TAG,
          mono: true,
        },
        {
          label: "Nameservers",
          value: "anderson · jessica",
          mono: true,
        },
        { section: "Setup" },
        {
          label: "DNS",
          value: `${cf?.dnsRecordCount ?? "—"} / ${cf?.dnsRecordsCap ?? 200} records`,
        },
        {
          label: "Proxy",
          value: "Orange-cloud on @ + www",
        },
        {
          label: "SSL",
          value: "Universal edge cert + Origin CA at Caddy",
        },
        {
          label: "Email",
          value: `${cf?.emailRoutingRuleCount ?? "—"} / ${cf?.emailRoutingRulesCap ?? 200} rules · catch-all → ${ownerEmail}`,
        },
        {
          label: "DMARC",
          value: "p=none (since 2026-05-22, → quarantine in 2wk)",
        },
        {
          label: "WAF",
          value: "Defaults · DDoS unmetered",
        },
      ]}
    >
      {cf?.r2BillingSummary && (
        <BillReconciliationBlock data={cf.r2BillingSummary} />
      )}
    </AccountBox>
  )
}

// Format a count + cap into "X / Y" with sensible "—" fallback. Used
// for DNS records and Email Routing rules where the cap is hard.
function renderCapMetric(count: number | null | undefined, cap: number): string {
  if (count === null || count === undefined) return "—"
  return `${count} / ${cap}`
}

// One-decimal signed-percent for the bill reconciliation table — we
// want sub-percent visibility there since the gap signal is the whole
// point of the panel.
function formatDriftPrecise(pct: number): string {
  if (pct === 0) return "0.0%"
  if (pct >= 999) return "+∞"
  const sign = pct > 0 ? "+" : ""
  return `${sign}${pct.toFixed(1)}%`
}

// 4-decimal USD for the bill reconciliation table — we're comparing
// fractional-cent totals here and need to see the difference.
function formatUsdPrecise(n: number): string {
  const sign = n < 0 ? "-$" : "$"
  return `${sign}${Math.abs(n).toFixed(4)}`
}

// Signed 4-decimal USD for the drift column.
function formatUsdSigned(n: number): string {
  if (n === 0) return "$0.0000"
  const sign = n < 0 ? "-$" : "+$"
  return `${sign}${Math.abs(n).toFixed(4)}`
}

// "5h ago", "yesterday", "3 days ago" — relative time for the
// last-cron-run line. Falls back to ISO if the value's bad.
function formatRelativeTime(iso: string): string {
  try {
    const then = new Date(iso).getTime()
    const now = Date.now()
    const sec = Math.max(0, Math.round((now - then) / 1000))
    if (sec < 60) return "just now"
    if (sec < 3600) return `${Math.round(sec / 60)}m ago`
    if (sec < 86400) return `${Math.round(sec / 3600)}h ago`
    const days = Math.round(sec / 86400)
    if (days === 1) return "yesterday"
    if (days < 30) return `${days} days ago`
    return new Date(iso).toLocaleDateString()
  } catch {
    return iso
  }
}

type BillReconciliationData = {
  periodStart: string
  periodEnd: string
  userBucket: string
  oursBytes: number
  cfBytes: number
  oursCostUsd: number
  cfCostUsd: number
  driftUsd: number
  driftPct: number
  lastCronRun: {
    ranAt: string
    periodLabel: string | null
    driftPct: number | null
    driftUsd: number | null
    alerted: boolean
  } | null
}

function BillReconciliationBlock({ data }: { data: BillReconciliationData }) {
  // Date range header: "May 1 – 25" for same-month, expands for
  // cross-month / cross-year so the range stays unambiguous.
  const start = new Date(data.periodStart)
  const end = new Date(data.periodEnd)
  const sameYear = start.getUTCFullYear() === end.getUTCFullYear()
  const sameMonth = sameYear && start.getUTCMonth() === end.getUTCMonth()
  const monthFmt = (d: Date) => d.toLocaleString("en-US", { month: "short", timeZone: "UTC" })
  const monthDayFmt = (d: Date) => `${monthFmt(d)} ${d.getUTCDate()}`
  const monthDayYearFmt = (d: Date) => `${monthFmt(d)} ${d.getUTCDate()}, ${d.getUTCFullYear()}`
  let periodLabel: string
  if (sameMonth) {
    periodLabel = `${monthDayFmt(start)} – ${end.getUTCDate()}`
  } else if (sameYear) {
    periodLabel = `${monthDayFmt(start)} – ${monthDayFmt(end)}`
  } else {
    periodLabel = `${monthDayYearFmt(start)} – ${monthDayYearFmt(end)}`
  }

  return (
    <div className="border border-border mb-6 last:mb-0">
      <div className="px-3 py-2">
        <div className="flex items-baseline justify-between gap-4 mb-2">
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold">
            Bill reconciliation · {periodLabel}
          </div>
          <div className="text-xs flex items-baseline gap-2 min-w-0">
            <span className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold shrink-0">
              Last cron run
            </span>
            <span className="truncate">
              {data.lastCronRun ? (
                <>
                  <span className="font-mono">
                    {formatRelativeTime(data.lastCronRun.ranAt)}
                  </span>
                  {data.lastCronRun.periodLabel && (
                    <>
                      {" · "}
                      <span className="text-muted-foreground">period </span>
                      <span className="font-mono">
                        {data.lastCronRun.periodLabel}
                      </span>
                    </>
                  )}
                  {data.lastCronRun.driftPct !== null && (
                    <>
                      {" · "}
                      <span className="text-muted-foreground">drift </span>
                      <span className="font-mono">
                        {formatDriftPrecise(data.lastCronRun.driftPct)}
                      </span>
                    </>
                  )}
                  {data.lastCronRun.alerted && (
                    <span className="ml-2 text-yellow-600 font-semibold">
                      ⚠ alerted
                    </span>
                  )}
                </>
              ) : (
                <span className="text-muted-foreground">
                  never · first fires on the 4th of next month at 04:00 UTC
                </span>
              )}
            </span>
          </div>
        </div>
        <table className="w-full text-sm font-mono tabular-nums">
          <thead className="text-[10px] uppercase tracking-wider text-muted-foreground font-sans">
            <tr>
              <th className="text-left font-semibold pb-1">Item</th>
              <th className="text-right font-semibold pb-1">Ours</th>
              <th className="text-right font-semibold pb-1">B2</th>
              <th className="text-right font-semibold pb-1">Drift</th>
            </tr>
          </thead>
          <tbody>
            <tr className="border-t border-border">
              <td className="py-1 font-sans">Storage bytes</td>
              <td className="text-right py-1">
                {data.oursBytes.toLocaleString()}
              </td>
              <td className="text-right py-1">
                {data.cfBytes.toLocaleString()}
              </td>
              <td className="text-right py-1">
                {formatDriftPrecise(data.driftPct)}
              </td>
            </tr>
            <tr className="border-t border-border">
              <td className="pt-1 font-sans font-semibold">
                Estimated period cost
              </td>
              <td className="text-right pt-1 font-semibold">
                {formatUsdPrecise(data.oursCostUsd)}
              </td>
              <td className="text-right pt-1 font-semibold">
                {formatUsdPrecise(data.cfCostUsd)}
              </td>
              <td className="text-right pt-1 font-semibold">
                {formatUsdSigned(data.driftUsd)}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ------------------------------------------------------------------
// StripeAccountBox — pulls live account / balance / payouts / disputes
// from /api/admin/stripe-account (which is a thin wrapper around
// the Stripe REST API). Single-fetch on mount, no polling — Stripe
// state changes on the order of minutes-to-hours, not seconds.
// ------------------------------------------------------------------

type StripeAccountInfo = {
  id: string | null
  country: string | null
  defaultCurrency: string | null
  chargesEnabled: boolean | null
  payoutsEnabled: boolean | null
  detailsSubmitted: boolean | null
  businessName: string | null
  supportEmail: string | null
  supportPhone: string | null
  supportUrl: string | null
  url: string | null
  hasLogo: boolean
  hasIcon: boolean
  primaryColor: string | null
  payoutSchedule: string | null
  payoutStatementDescriptor: string | null
  minimumBalanceUsd: number | null
}
type StripeBalanceLeg = { amountUsd: number; currency: string }
type StripeExternalAccount = {
  bankName: string | null
  last4: string | null
  currency: string | null
  country: string | null
  object: string | null
}
type StripeRecentPayout = {
  id: string
  amountUsd: number
  currency: string
  status: string
  arrivalDate: string | null
  method: string | null
}
type StripeAccountSnapshot = {
  configured: boolean
  errors: string[]
  account: StripeAccountInfo | null
  balance: { available: StripeBalanceLeg[]; pending: StripeBalanceLeg[] } | null
  externalAccount: StripeExternalAccount | null
  recentPayouts: StripeRecentPayout[]
  disputes: { openCount: number | null }
}

function useStripeAccount(): StripeAccountSnapshot | null {
  const [data, setData] = React.useState<StripeAccountSnapshot | null>(null)
  React.useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const r = await fetch("/api/admin/stripe-account", {
          credentials: "include",
        })
        if (!r.ok) {
          if (!cancelled)
            setData({
              configured: false,
              errors: [`HTTP ${r.status}`],
              account: null,
              balance: null,
              externalAccount: null,
              recentPayouts: [],
              disputes: { openCount: null },
            })
          return
        }
        const json = (await r.json()) as StripeAccountSnapshot
        if (!cancelled) setData(json)
      } catch (e) {
        if (!cancelled)
          setData({
            configured: false,
            errors: [e instanceof Error ? e.message : String(e)],
            account: null,
            balance: null,
            externalAccount: null,
            recentPayouts: [],
            disputes: { openCount: null },
          })
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])
  return data
}

// Hardcoded as a fallback so the AccountBox identity rows render
// correctly even when /api/admin/stripe-account is down. Matches the
// Supplied by /api/admin/identifiers now. Update when Stripe ever
// reassigns it (which they don't, but documenting the dependency).

function StripeAccountBox() {
  const { stripeAccountId: STRIPE_ACCOUNT_ID, ownerEmail } =
    useAdminIdentifiers()
  const data = useStripeAccount()
  const a = data?.account ?? null
  const bal = data?.balance ?? null

  // Status: active iff charges + payouts enabled AND a bank account
  // is on file. Anything short of that is a warning, since the merchant
  // account isn't fully ready to take money. Down only when the API
  // call itself failed.
  let status: "active" | "warning" | "down" = "down"
  let statusDetail: string | undefined
  if (data && data.errors.length > 0 && !a) {
    status = "down"
    statusDetail = data.errors.join(" · ")
  } else if (a) {
    const missing: string[] = []
    if (!a.chargesEnabled) missing.push("charges disabled")
    if (!a.payoutsEnabled) missing.push("payouts disabled")
    if (!data?.externalAccount) missing.push("no bank account on file")
    if (!a.supportEmail) missing.push("no support email")
    if (!a.hasIcon) missing.push("no icon")
    if (!a.hasLogo) missing.push("no logo")
    status = missing.length === 0 ? "active" : "warning"
    if (missing.length > 0) statusDetail = missing.join(" · ")
  }

  const usdAvail = bal?.available.find((b) => b.currency === "usd")
  const usdPending = bal?.pending.find((b) => b.currency === "usd")
  const balanceCard = usdAvail
    ? formatUsd(usdAvail.amountUsd)
    : data
      ? "$0.00"
      : "—"
  const pendingCard = usdPending ? formatUsd(usdPending.amountUsd) : "—"

  const disputesOpen = data?.disputes.openCount
  const disputesCard =
    disputesOpen == null
      ? "—"
      : disputesOpen > 0
        ? `${disputesOpen} OPEN`
        : "0 · clean"

  const bankCard = data?.externalAccount
    ? `${data.externalAccount.bankName ?? "Bank"} ····${data.externalAccount.last4 ?? "??"}`
    : data
      ? "not connected"
      : "—"

  // Details: every config flag we surface in the dashboard.
  const dot = (ok: boolean | null | undefined) =>
    ok ? (
      <span className="flex items-center gap-2">
        <span className="inline-block w-1.5 h-1.5 rounded-full bg-green-500" />
        Yes
      </span>
    ) : (
      <span className="flex items-center gap-2 text-yellow-500">
        <span className="inline-block w-1.5 h-1.5 rounded-full bg-yellow-500" />
        No
      </span>
    )

  const details: { label: string; value: React.ReactNode; mono?: boolean }[] = [
    { label: "Account ID", value: a?.id ?? STRIPE_ACCOUNT_ID, mono: true },
    { label: "Login", value: ownerEmail, mono: true },
    { label: "Country", value: a?.country?.toUpperCase() ?? "—" },
    { label: "Default currency", value: a?.defaultCurrency?.toUpperCase() ?? "—" },
    { label: "Business name", value: a?.businessName ?? "—" },
    { label: "Charges enabled", value: dot(a?.chargesEnabled) },
    { label: "Payouts enabled", value: dot(a?.payoutsEnabled) },
    { label: "Onboarding complete", value: dot(a?.detailsSubmitted) },
    { label: "Support email", value: a?.supportEmail ?? "—", mono: true },
    {
      label: "Bank on file",
      value: data?.externalAccount
        ? `${data.externalAccount.bankName ?? "Bank"} ····${data.externalAccount.last4 ?? "??"} (${data.externalAccount.currency?.toUpperCase() ?? "?"})`
        : "Not connected — money will queue at Stripe with nowhere to land",
      mono: !!data?.externalAccount,
    },
    { label: "Payout schedule", value: a?.payoutSchedule ?? "—" },
    {
      label: "Payout descriptor",
      value: a?.payoutStatementDescriptor ?? "—",
      mono: true,
    },
    {
      label: "Minimum balance",
      value:
        a?.minimumBalanceUsd != null
          ? `${formatUsd(a.minimumBalanceUsd)} (negative-balance buffer)`
          : "Not set",
    },
    {
      label: "Recent payouts",
      value:
        data && data.recentPayouts.length > 0 ? (
          <div className="space-y-1">
            {data.recentPayouts.slice(0, 5).map((p) => (
              <div key={p.id} className="font-mono text-xs">
                {formatUsd(p.amountUsd)} {p.currency.toUpperCase()} · {p.status}
                {p.arrivalDate ? ` · arrives ${p.arrivalDate.slice(0, 10)}` : ""}
              </div>
            ))}
          </div>
        ) : (
          "None yet"
        ),
    },
  ]

  if (data && data.errors.length > 0) {
    details.push({
      label: "API call errors",
      value: (
        <span className="text-yellow-500">{data.errors.join(" · ")}</span>
      ),
    })
  }

  return (
    <AccountBox
      name="Stripe"
      consoleUrl="https://dashboard.stripe.com"
      consoleLabel="Dashboard"
      accountUrl="https://dashboard.stripe.com/settings/account"
      accountLabel="Account"
      status={status}
      statusDetail={statusDetail}
      keyMetrics={[
        { label: "USD available", value: balanceCard },
        { label: "USD pending", value: pendingCard },
        { label: "Open disputes", value: disputesCard },
        { label: "Bank on file", value: bankCard },
      ]}
      notes={[
        "Chargebacks cost a non-recoverable dispute fee regardless of who wins. Radar Early Fraud Warning webhooks (`radar.early_fraud_warning.created`) give us a short window to proactively refund BEFORE the cardholder formally disputes — avoiding the fee entirely. Currently logged + alerted; auto-refund policy can be added later.",
        "Negative-balance buffer (see Minimum balance row below) is configured so Stripe never sends a payout that would leave us in the red — protects against dispute-driven negative-balance situations after the bank account is already emptied.",
      ]}
      details={details}
    />
  )
}

// ------------------------------------------------------------------
// MercuryAccountBox — pulls live balance + recent transactions from
// /api/admin/mercury-account. Pinned server-side to the single ARCHIVE336
// account (the user has multiple Mercury accounts across projects);
// the API key only ever needs read scope.
// ------------------------------------------------------------------

type MercuryAccountInfo = {
  name: string | null
  nickname: string | null
  kind: string | null
  status: string | null
  currentBalance: number | null
  availableBalance: number | null
  routingNumber: string | null
  last4: string | null
}

type MercuryTransaction = {
  id: string | null
  amount: number | null
  status: string | null
  createdAt: string | null
  kind: string | null
  counterparty: string | null
  note: string | null
}

type MercurySnapshot = {
  configured: boolean
  account: MercuryAccountInfo | null
  recentTransactions: MercuryTransaction[]
  errors: string[]
}

function useMercuryAccount(): MercurySnapshot | null {
  const [data, setData] = React.useState<MercurySnapshot | null>(null)
  React.useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const r = await fetch("/api/admin/mercury-account", {
          credentials: "include",
        })
        if (!r.ok) return
        const json = (await r.json()) as MercurySnapshot
        if (!cancelled) setData(json)
      } catch {
        // network error — leave data null so cards render "—"
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])
  return data
}

function MercuryAccountBox() {
  const { mercuryAccountId, ownerEmail } = useAdminIdentifiers()
  const data = useMercuryAccount()
  const a = data?.account ?? null

  let status: "active" | "warning" | "down" = "active"
  let statusDetail: string | undefined
  if (data && !data.configured) {
    status = "warning"
    statusDetail = "MERCURY_API_KEY not set"
  } else if (data && data.errors.length > 0) {
    status = "warning"
    statusDetail = data.errors.join(" · ")
  } else if (a?.status && a.status !== "active") {
    // Mercury can flag accounts (e.g. "frozen", "restricted"). Catch
    // any non-active state and surface it on the pill so an
    // unexpected status change is impossible to miss.
    status = "warning"
    statusDetail = `Mercury account status: ${a.status}`
  }

  const availableCard =
    a?.availableBalance != null
      ? formatUsd(a.availableBalance)
      : data && !data.configured
        ? "— (set MERCURY_API_KEY)"
        : "—"
  const pendingDiff =
    a?.availableBalance != null && a?.currentBalance != null
      ? a.currentBalance - a.availableBalance
      : null
  const pendingCard = pendingDiff != null ? formatUsd(pendingDiff) : "—"

  const recentTxCount = data?.recentTransactions.length ?? 0
  const latestTx = data?.recentTransactions[0]
  const lastActivityCard = latestTx?.createdAt
    ? latestTx.createdAt.slice(0, 10)
    : recentTxCount === 0 && data?.configured
      ? "None yet"
      : "—"

  return (
    <AccountBox
      name="Mercury"
      consoleUrl={`https://app.mercury.com/accounts/depository/${mercuryAccountId}`}
      consoleLabel="Account"
      accountUrl="https://app.mercury.com/settings/banking/api-tokens"
      accountLabel="API tokens"
      status={status}
      statusDetail={statusDetail}
      keyMetrics={[
        { label: "USD available", value: availableCard },
        { label: "USD pending", value: pendingCard },
        { label: "Recent transactions", value: recentTxCount.toString() },
        { label: "Last activity", value: lastActivityCard },
      ]}
      notes={[
        "This is the destination for Stripe payouts. The Stripe box's 'Bank on file' card should show the same last 4 digits — if they ever diverge, payouts are landing in the wrong account.",
        "If Mercury Treasury (higher-yield sub-account) is enabled, the balance shown here is only the operating portion. The Mercury dashboard's sub-account view is the full picture.",
      ]}
      details={[
        {
          label: "Login",
          value: ownerEmail,
          mono: true,
        },
        { label: "Account holder", value: "Afraaz LLC" },
        { label: "Account name", value: a?.name ?? "ARCHIVE336" },
        { label: "Bank", value: "Choice Financial Group (Fargo, ND)" },
        {
          label: "Type",
          value: a?.kind ?? "checking",
          mono: true,
        },
        {
          label: "Status",
          value: a?.status ?? "—",
          mono: true,
        },
        {
          label: "Last 4",
          value: a?.last4 ?? "2640",
          mono: true,
        },
        {
          label: "Routing number",
          value: a?.routingNumber ?? "091311229",
          mono: true,
        },
        {
          label: "Account ID",
          value: mercuryAccountId,
          mono: true,
        },
        {
          label: "API key",
          value: "MERCURY_API_KEY in /opt/aether/.env (mode 600)",
          mono: true,
        },
        {
          label: "Recent transactions",
          value:
            data && data.recentTransactions.length > 0 ? (
              <div className="space-y-1">
                {data.recentTransactions.slice(0, 5).map((t, i) => {
                  const amt =
                    t.amount != null
                      ? `${t.amount >= 0 ? "+" : "-"}$${Math.abs(t.amount).toFixed(2)}`
                      : "—"
                  const who =
                    t.counterparty || t.note || t.kind || "—"
                  const date = t.createdAt
                    ? t.createdAt.slice(0, 10)
                    : "—"
                  const txStatus = t.status ?? "—"
                  return (
                    <div key={t.id ?? i} className="font-mono text-xs">
                      {amt} · {who} · {txStatus} · {date}
                    </div>
                  )
                })}
              </div>
            ) : (
              "None yet"
            ),
        },
      ]}
    />
  )
}

function SentryAccountBox() {
  const { providerLogin } = useAdminIdentifiers()
  const health = useServiceHealth()
  const sentry = health.sentry
  const status = sentry?.status ?? "active"

  const eventsCard =
    sentry?.eventsThisMonth === undefined
      ? "—"
      : sentry.eventsThisMonth === null
        ? "— (set SENTRY_AUTH_TOKEN)"
        : `${sentry.eventsThisMonth} / ${sentry.monthlyCap}`

  return (
    <AccountBox
      name="Sentry"
      consoleUrl="https://archive336.sentry.io/issues/"
      consoleLabel="Console"
      accountUrl="https://sentry.io/settings/account/"
      accountLabel="Account"
      status={status}
      statusDetail={sentry?.warnings?.[0]}
      keyMetrics={[
        { label: "Events this month", value: eventsCard },
        { label: "Projects", value: "2 (backend + frontend)" },
      ]}
      notes={[
        "One cliff: 5,000 errors/mo. After that Sentry silently stops recording — unlike a cap that fails user-visible actions, Sentry just goes quiet. We LOSE visibility right when we'd need it most (an error storm is usually how you'd hit 5k). The Events this month card warns at 80%.",
        "Live count for the Events card comes from Sentry's stats_v2 API. Requires a SENTRY_AUTH_TOKEN with org:read + event:read scopes in /opt/aether/.env. Until that's set, the card shows '— (set SENTRY_AUTH_TOKEN)'. Generate at sentry.io/settings/account/api/auth-tokens/ — read-only.",
      ]}
      details={[
        { label: "Login", value: providerLogin, mono: true },
        { label: "Organization", value: "archive336", mono: true },
        {
          label: "Projects",
          value: "python-fastapi (backend) · javascript-react (frontend)",
          mono: true,
        },
        {
          label: "Credentials",
          value: "SENTRY_DSN in /opt/aether/.env · browser DSN inlined in vite.config.ts (ships in the public JS bundle by design)",
          mono: true,
        },
      ]}
    />
  )
}

function ResendAccountBox() {
  const { providerLogin } = useAdminIdentifiers()
  const health = useServiceHealth()
  const resend = health.resend
  const status = resend?.status ?? "active"

  const today =
    resend !== undefined
      ? `${resend.sendsToday} / ${resend.dailyCap}`
      : "—"
  const thisMonth =
    resend !== undefined
      ? `${resend.sendsThisMonth} / ${resend.monthlyCap}`
      : "—"

  return (
    <AccountBox
      name="Resend"
      consoleUrl="https://resend.com/emails"
      consoleLabel="Console"
      accountUrl="https://resend.com/settings"
      accountLabel="Account"
      status={status}
      statusDetail={resend?.warnings?.[0]}
      keyMetrics={[
        { label: "Today", value: today },
        { label: "This month", value: thisMonth },
      ]}
      notes={[
        "Two cliffs to know about: 3,000 sends/mo OR 100/day. Either one trips and Resend hard-rejects further sends until reset. User-facing actions that depend on email (signup verification, password reset) would fail silently. The Today and This Month cards warn at 80% of either cap.",
        "Send count comes from our own EmailSendLog table, not Resend's API (avoids needing a higher-scope API key). If you ever rotate Resend workspaces, the local count can drift above what Resend's dashboard shows — informational, not a bug.",
      ]}
      details={[
        { label: "Workspace", value: "Afraaz LLC" },
        { label: "Login", value: providerLogin, mono: true },
        { label: "Sender", value: "noreply@archive336.com", mono: true },
        {
          label: "Domain auth",
          value:
            "DKIM + SPF + DMARC published via Cloudflare DNS (auto-configured)",
        },
        {
          label: "API key",
          value: "RESEND_API_KEY in /opt/aether/.env (mode 600)",
          mono: true,
        },
        {
          label: "Wired triggers",
          value:
            "send_email_verification · send_password_reset · send_account_deletion_confirmation · send_payment_failed · send_oauth_disconnected",
        },
      ]}
    />
  )
}

function HetznerAccountBox() {
  const { originIpv4, originIpv6, sshKeyPath, providerLogin } =
    useAdminIdentifiers()
  const health = useServiceHealth()
  const hetzner = health.hetzner
  const status = hetzner?.status ?? "active"

  // Surface the live disk + memory readings as key metric cards so
  // the operator can see at a glance whether we're close to needing
  // to scale up.
  const diskCard = hetzner?.diskFreeGb
    ? `${hetzner.diskFreeGb} GB free`
    : "—"
  const memCard =
    hetzner?.memUsedPct !== undefined && hetzner?.memUsedPct !== null
      ? `${hetzner.memUsedPct}% used`
      : "—"
  // Bandwidth: format "X.XX GB / 1 TB" or similar. Scales the unit on
  // the outgoing side so we don't show "0.00 GB" for small numbers.
  const bandwidthCard = hetzner?.bandwidth
    ? formatBandwidth(
        hetzner.bandwidth.outgoingBytes,
        hetzner.bandwidth.includedBytes,
      )
    : "—"

  // Unexpected-resource watchers. We expect 0 of each — anything
  // positive is a yellow warning since each accrues real cost (or
  // could). Snapshots: ~$0.012/GB-mo. Volumes: $0.044/GB-mo.
  // Floating IPs: $0.50/mo. Load balancers: $5+/mo.
  const snapshotValue = renderResourceWatch(
    hetzner?.manualSnapshots,
    "https://console.hetzner.com/projects/14375318/security/snapshots",
  )
  const volumeValue = renderResourceWatch(
    hetzner?.volumes,
    "https://console.hetzner.com/projects/14375318/volumes",
  )
  const floatingIpValue = renderResourceWatch(
    hetzner?.floatingIps,
    "https://console.hetzner.com/projects/14375318/floating_ips",
  )
  const loadBalancerValue = renderResourceWatch(
    hetzner?.loadBalancers,
    "https://console.hetzner.com/projects/14375318/load_balancers",
  )

  return (
    <AccountBox
      name="Hetzner"
      consoleUrl="https://console.hetzner.com"
      consoleLabel="Console"
      accountUrl="https://accounts.hetzner.com"
      accountLabel="Account"
      status={status}
      statusDetail={hetzner?.warnings?.[0]}
      keyMetrics={[
        { label: "Tier", value: "CPX21 · Ashburn" },
        { label: "Disk", value: diskCard },
        { label: "Memory", value: memCard },
        { label: "Bandwidth", value: bandwidthCard },
      ]}
      notes={[
        "Don't create manual snapshots, additional volumes, floating IPs, or load balancers in the Hetzner console. We don't use any of them, and each accrues spend if created. The four rows below poll Hetzner's API every 30s and turn yellow if any unexpected resource appears.",
      ]}
      details={[
        { label: "Server", value: "archive336-1 · #128288947" },
        { label: "IPv4", value: originIpv4, mono: true },
        { label: "IPv6", value: originIpv6, mono: true },
        { label: "SSH key", value: sshKeyPath, mono: true },
        { label: "Login", value: providerLogin, mono: true },
        {
          label: "Hetzner backups",
          value: "Live · 7 rolling daily snapshots",
        },
        {
          label: "Litestream backup",
          value: "Live · 60s RPO to R2 (aether-archive-backups bucket)",
        },
        {
          label: "Manual snapshots",
          value: snapshotValue,
        },
        {
          label: "Volumes",
          value: volumeValue,
        },
        {
          label: "Floating IPs",
          value: floatingIpValue,
        },
        {
          label: "Load balancers",
          value: loadBalancerValue,
        },
      ]}
    />
  )
}

type KeyMetric = { label: string; value: string }
type DetailRow =
  | {
      label: string
      // React.ReactNode so rows can render rich values (links, status
      // dots, warnings) in addition to plain strings.
      value: React.ReactNode
      mono?: boolean
    }
  // Section-divider variant: renders as a small uppercase heading
  // inside the details table, useful for grouping related rows when
  // a box has more than ~5 of them.
  | { section: string }
type CostLineItem = {
  label: string
  monthlyUsd: number
  yearlyUsd: number
  note?: string
}
type MoneySummary = {
  attributable: boolean
  attributableLabel: string
  // Cost breakdown — every line item that contributes to this
  // account's bill. Rendered as a table with monthly + yearly +
  // an auto-summed total row.
  costs: CostLineItem[]
  notes?: string[]
}

// Annual price of one $1/year membership. Used to compute the
// "users needed to break even" count for fixed-overhead accounts.
const MEMBERSHIP_YEARLY_USD = 1.0


// Mono-font value with click-to-copy. Used on AccountBox detail
// rows where the value is an ID, account number, or anything else
// you'd commonly need to paste into a console / support chat / env
// file. Shows a transient "copied" pill when the click succeeds so
// the user sees the action took.
function CopyableMono({ value }: { value: string }) {
  const [copied, setCopied] = React.useState(false)
  const onClick = async (e: React.MouseEvent) => {
    e.preventDefault()
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1200)
    } catch {
      // Clipboard API can fail in private modes / unsupported envs;
      // silently no-op rather than throwing a toast for a UI nicety.
    }
  }
  return (
    <button
      type="button"
      onClick={onClick}
      title="Click to copy"
      className="font-mono cursor-pointer text-left hover:text-foreground transition-colors relative group"
    >
      {value}
      <span
        className={
          "ml-2 text-[10px] uppercase tracking-wider font-semibold " +
          (copied ? "text-emerald-500" : "text-muted-foreground/40 group-hover:text-muted-foreground")
        }
      >
        {copied ? "copied" : "copy"}
      </span>
    </button>
  )
}

function formatUsd(n: number): string {
  return `$${n.toFixed(2)}`
}

// Shared renderer for "we expect 0 of this resource" rows in the
// Hetzner box. Three states: unknown (HCLOUD_TOKEN missing), clean
// (count is 0), warning (count > 0 with a console link to clean up).
function renderResourceWatch(
  count: number | null | undefined,
  reviewUrl: string,
): React.ReactNode {
  if (count === undefined || count === null) {
    return (
      <span className="text-muted-foreground">
        — (set HCLOUD_TOKEN in .env to enable)
      </span>
    )
  }
  if (count === 0) {
    return (
      <span className="flex items-center gap-2">
        <span className="inline-block w-1.5 h-1.5 rounded-full bg-green-500" />
        0 · clean
      </span>
    )
  }
  return (
    <span className="flex items-center gap-2 text-yellow-500 font-semibold">
      <span className="inline-block w-1.5 h-1.5 rounded-full bg-yellow-500" />
      {count} · review &amp; delete in console{" "}
      <a
        href={reviewUrl}
        target="_blank"
        rel="noopener noreferrer"
        className="underline"
      >
        ↗
      </a>
    </span>
  )
}

// "5.4 GB / 1 TB" style. Outgoing side scales unit (B/KB/MB/GB) so
// very small numbers don't show as "0.00 GB". Included side is shown
// in TB at one decimal since Hetzner's tiers are sized in TB.
function formatBandwidth(outgoingBytes: number, includedBytes: number): string {
  const outStr =
    outgoingBytes < 1_000_000
      ? `${Math.round(outgoingBytes / 1_000)} KB`
      : outgoingBytes < 1_000_000_000
        ? `${(outgoingBytes / 1_000_000).toFixed(0)} MB`
        : `${(outgoingBytes / 1_000_000_000).toFixed(2)} GB`
  const incTb = includedBytes / 1_000_000_000_000
  const incStr =
    incTb >= 1 ? `${incTb.toFixed(0)} TB` : `${(includedBytes / 1_000_000_000).toFixed(0)} GB`
  return `${outStr} / ${incStr}`
}

function AccountBox({
  name,
  description,
  consoleUrl,
  consoleLabel,
  accountUrl,
  accountLabel,
  status,
  statusDetail,
  keyMetrics,
  notes,
  details,
  moneySummary,
  children,
}: {
  name: string
  description?: string
  consoleUrl?: string
  consoleLabel?: string
  accountUrl?: string
  accountLabel?: string
  status: "active" | "warning" | "down"
  statusDetail?: string
  keyMetrics: KeyMetric[]
  // New minimal-shape pattern: short "watch this" bullets only when
  // there's something operationally interesting. Three bullets max
  // typically. Optional — many vendors will have nothing to surface.
  notes?: string[]
  // Legacy verbose-detail-rows table. Kept optional so existing
  // call sites still compile during the gradual migration; new code
  // should prefer the simpler `notes` shape and let `STACK.md` carry
  // the long-form details.
  details?: DetailRow[]
  // Legacy "Money situation" sub-section. Kept optional during
  // migration; new code should put cost into the keyMetrics card
  // instead and skip this sub-section entirely.
  moneySummary?: MoneySummary
  // Optional custom block (e.g. CloudflareAccountBox's bill
  // reconciliation table). Rendered between `details` and
  // `moneySummary` so it sits with the operational data, not the
  // money block.
  children?: React.ReactNode
}) {
  return (
    <section className="border border-border p-6">
      {/* Header row: name + status pill on the left, action buttons on the
          right. No eyebrow so the H2 sits flush with the buttons. */}
      <div className="flex items-center justify-between gap-4 mb-4">
        <div className="flex items-center gap-3">
          <h2 className="text-2xl font-bold leading-none">{name}</h2>
          <StatusPill status={status} detail={statusDetail} />
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {accountUrl && (
            <a
              href={accountUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="text-[10px] uppercase tracking-wider font-semibold border border-border px-3 py-1.5 flex items-center gap-1.5"
            >
              {accountLabel ?? "Account"}
              <ExternalLink size={12} />
            </a>
          )}
          {consoleUrl && (
            <a
              href={consoleUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="text-[10px] uppercase tracking-wider font-semibold border border-border px-3 py-1.5 flex items-center gap-1.5"
            >
              {consoleLabel ?? "Console"}
              <ExternalLink size={12} />
            </a>
          )}
        </div>
      </div>

      {description && (
        <p className="text-sm text-muted-foreground leading-relaxed mb-5">
          {description}
        </p>
      )}

      {/* Key-metric grid. lg-breakpoint columns adapt to the card
          count so 4-card boxes don't leave an empty slot at the
          end of a 5-col grid. Mobile/tablet stay 2/3 cols
          regardless — cards there have plenty of room. */}
      <div
        className={
          "grid grid-cols-2 md:grid-cols-3 gap-3 mb-6 last:mb-0 " +
          (keyMetrics.length === 4
            ? "lg:grid-cols-4"
            : keyMetrics.length === 3
              ? "lg:grid-cols-3"
              : "lg:grid-cols-5")
        }
      >
        {keyMetrics.map((m) => (
          <Stat key={m.label} label={m.label} value={m.value} />
        ))}
      </div>

      {/* "Watch this" short notes — only render if the call site
          provided some. Designed for 1-3 bullets that materially
          matter operationally; longer explanations belong in
          STACK.md. */}
      {notes && notes.length > 0 && (
        <div className="border-l-2 border-border pl-3 space-y-1.5 mb-2 last:mb-0">
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold">
            Watch this
          </div>
          {notes.map((n, i) => (
            <p key={i} className="text-xs text-muted-foreground leading-relaxed">
              {n}
            </p>
          ))}
        </div>
      )}

      {/* Legacy verbose detail rows — only render if call site still
          passes `details`. New call sites should leave this empty
          and let STACK.md carry the long-form context.

          Two row shapes: a label/value pair (default), or a
          {section: "..."} divider that breaks the table into named
          groups. The first row never gets a top border; section
          dividers always force one. */}
      {details && details.length > 0 && (
        <div className="border border-border mb-6 last:mb-0">
          {details.map((d, i) => {
            const isSection = "section" in d
            const key = isSection ? `__section_${d.section}_${i}` : d.label
            const isLast = i === details.length - 1
            if (isSection) {
              return (
                <div
                  key={key}
                  className={
                    "px-3 py-1.5 bg-muted/30 text-[10px] uppercase tracking-wider " +
                    "text-muted-foreground font-semibold " +
                    (i > 0 ? "border-t border-border " : "") +
                    (!isLast ? "border-b border-border" : "")
                  }
                >
                  {d.section}
                </div>
              )
            }
            return (
              <div
                key={key}
                className={
                  "flex text-sm px-3 py-2 " +
                  (!isLast ? "border-b border-border" : "")
                }
              >
                <div className="w-44 shrink-0 text-[10px] uppercase tracking-wider text-muted-foreground font-semibold pt-0.5">
                  {d.label}
                </div>
                {d.mono && typeof d.value === "string" ? (
                  <CopyableMono value={d.value} />
                ) : (
                  <div className={d.mono ? "font-mono" : ""}>{d.value}</div>
                )}
              </div>
            )
          })}
        </div>
      )}

      {/* Optional custom block (e.g. bill reconciliation). Renders
          between the operational data and the legacy money block. */}
      {children}

      {/* Legacy money-summary sub-section — only render if call site
          still passes `moneySummary`. New code puts cost in the
          keyMetrics card instead and skips this. */}
      {moneySummary && (
        <>
          <SectionHeader>Money situation</SectionHeader>
          <MoneyBlock summary={moneySummary} />
        </>
      )}
    </section>
  )
}

function MoneyBlock({ summary }: { summary: MoneySummary }) {
  const totalMonthly = summary.costs.reduce((s, c) => s + c.monthlyUsd, 0)
  const totalYearly = summary.costs.reduce((s, c) => s + c.yearlyUsd, 0)
  // How many $1/year subs we'd need to cover this account's full
  // yearly cost. Ceiling — partial subs aren't a thing.
  const breakEvenSubs = Math.ceil(totalYearly / MEMBERSHIP_YEARLY_USD)

  return (
    <div className="border border-border">
      {/* Cost breakdown table */}
      <div className="px-3 py-2 border-b border-border">
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-2">
          Cost breakdown
        </div>
        <table className="w-full text-sm font-mono tabular-nums">
          <thead className="text-[10px] uppercase tracking-wider text-muted-foreground font-sans">
            <tr>
              <th className="text-left font-semibold pb-1">Line item</th>
              <th className="text-right font-semibold pb-1">Monthly</th>
              <th className="text-right font-semibold pb-1">Yearly</th>
            </tr>
          </thead>
          <tbody>
            {summary.costs.map((c) => (
              <tr key={c.label}>
                <td className="py-0.5 font-sans">
                  {c.label}
                  {c.note && (
                    <span className="text-muted-foreground font-sans">
                      {" "}
                      · {c.note}
                    </span>
                  )}
                </td>
                <td className="text-right py-0.5">{formatUsd(c.monthlyUsd)}</td>
                <td className="text-right py-0.5">{formatUsd(c.yearlyUsd)}</td>
              </tr>
            ))}
            <tr className="border-t border-border">
              <td className="pt-1 font-sans font-semibold">Total</td>
              <td className="text-right pt-1 font-semibold">
                {formatUsd(totalMonthly)}
              </td>
              <td className="text-right pt-1 font-semibold">
                {formatUsd(totalYearly)}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      {/* Classification + break-even */}
      <div className="flex text-sm px-3 py-2 border-b border-border">
        <div className="w-44 shrink-0 text-[10px] uppercase tracking-wider text-muted-foreground font-semibold pt-0.5">
          User-attributable
        </div>
        <div className="flex items-center gap-2">
          <span
            className={
              "inline-block w-1.5 h-1.5 rounded-full " +
              (summary.attributable ? "bg-green-500" : "bg-yellow-500")
            }
          />
          <span>
            {summary.attributable ? "Yes" : "No"} ·{" "}
            <span className="text-muted-foreground">
              {summary.attributableLabel}
            </span>
          </span>
        </div>
      </div>
      <div className="flex text-sm px-3 py-2 border-b border-border">
        <div className="w-44 shrink-0 text-[10px] uppercase tracking-wider text-muted-foreground font-semibold pt-0.5">
          Break-even
        </div>
        <div>
          <span className="font-mono tabular-nums font-semibold">
            {breakEvenSubs.toLocaleString()}
          </span>{" "}
          <span className="text-muted-foreground">
            × $1/yr subscriptions to cover this account
          </span>
        </div>
      </div>

      {summary.notes?.map((note, i) => (
        <div
          key={i}
          className={
            "text-sm px-3 py-2 text-muted-foreground leading-relaxed " +
            (i < (summary.notes?.length ?? 0) - 1
              ? "border-b border-border"
              : "")
          }
        >
          {note}
        </div>
      ))}
    </div>
  )
}

function StatusPill({
  status,
  detail,
}: {
  status: "active" | "warning" | "down"
  detail?: string
}) {
  const dotColor =
    status === "active"
      ? "bg-green-500"
      : status === "warning"
        ? "bg-yellow-500"
        : "bg-red-500"
  const label =
    status === "active" ? "Active" : status === "warning" ? "Warning" : "Down"
  return (
    <div
      className="flex items-center gap-1.5 border border-border px-2 py-0.5 text-[10px] uppercase tracking-wider font-semibold"
      title={detail}
    >
      <span className={"inline-block w-1.5 h-1.5 rounded-full " + dotColor} />
      {label}
    </div>
  )
}

// ------------------------------------------------------------------
// Helpers
// ------------------------------------------------------------------

function SectionHeader({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">
      {children}
    </div>
  )
}

function Stat({
  icon,
  label,
  value,
}: {
  icon?: React.ReactNode
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

function Th({
  children,
  className,
  title,
}: {
  children: React.ReactNode
  className?: string
  title?: string
}) {
  return (
    <th
      className={
        "text-left font-semibold px-3 py-2 " + (className ?? "")
      }
      title={title}
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
  return <td className={"px-3 py-2 " + (className ?? "")}>{children}</td>
}

function useDebounced<T>(value: T, delay: number): T {
  const [v, setV] = React.useState(value)
  React.useEffect(() => {
    const t = setTimeout(() => setV(value), delay)
    return () => clearTimeout(t)
  }, [value, delay])
  return v
}

// ------------------------------------------------------------------
// Tiny markdown renderer for the Stack tab. Supports the subset
// STACK.md actually uses: # / ## / ### headings, bullet lists,
// **bold**, `code`, [text](url) links, and the one pricing table.
// Anything fancier should drive a real markdown lib instead.
// ------------------------------------------------------------------

type Block =
  | { kind: "h"; level: 1 | 2 | 3; text: string }
  | { kind: "p"; text: string }
  | { kind: "ul"; items: string[] }
  | { kind: "table"; head: string[]; rows: string[][] }
  | { kind: "hr" }
  | { kind: "blank" }

function parseMarkdown(src: string): Block[] {
  const lines = src.split("\n")
  const blocks: Block[] = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    if (!line.trim()) {
      blocks.push({ kind: "blank" })
      i++
      continue
    }
    const h = /^(#{1,3})\s+(.+)$/.exec(line)
    if (h) {
      blocks.push({
        kind: "h",
        level: h[1].length as 1 | 2 | 3,
        text: h[2].trim(),
      })
      i++
      continue
    }
    // Horizontal rule: 3+ dashes on their own line. Used to visually
    // separate the big At-a-glance / Recurring / Variable / Free
    // sections in STACK.md.
    if (/^-{3,}$/.test(line.trim())) {
      blocks.push({ kind: "hr" })
      i++
      continue
    }
    if (line.startsWith("- ")) {
      const items: string[] = []
      while (i < lines.length && lines[i].startsWith("- ")) {
        // Continuation lines indented by 2+ spaces fold into the
        // current item so multi-line bullets read as one paragraph.
        let item = lines[i].slice(2)
        i++
        while (i < lines.length && /^\s{2,}\S/.test(lines[i])) {
          item += " " + lines[i].trim()
          i++
        }
        items.push(item)
      }
      blocks.push({ kind: "ul", items })
      continue
    }
    if (line.startsWith("|") && i + 1 < lines.length && /^\|[\s\-|:]+\|$/.test(lines[i + 1])) {
      const head = splitTableRow(line)
      i += 2 // skip header + separator
      const rows: string[][] = []
      while (i < lines.length && lines[i].startsWith("|")) {
        rows.push(splitTableRow(lines[i]))
        i++
      }
      blocks.push({ kind: "table", head, rows })
      continue
    }
    // Regular paragraph: gather contiguous non-blank lines that aren't
    // headings/lists/tables.
    let para = line
    i++
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^#{1,3}\s/.test(lines[i]) &&
      !lines[i].startsWith("- ") &&
      !lines[i].startsWith("|")
    ) {
      para += " " + lines[i].trim()
      i++
    }
    blocks.push({ kind: "p", text: para })
  }
  return blocks
}

function splitTableRow(line: string): string[] {
  return line
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((c) => c.trim())
}

// Render inline markup: **bold**, `code`, [text](url).
// Naive but adequate for our STACK.md content — no nested markup,
// no escapes, no images.
function renderInline(s: string): React.ReactNode[] {
  const out: React.ReactNode[] = []
  // Token regex tries each pattern in turn.
  const re = /(\*\*[^*]+\*\*)|(`[^`]+`)|(\[[^\]]+\]\([^)]+\))/g
  let last = 0
  let m: RegExpExecArray | null
  let key = 0
  while ((m = re.exec(s)) !== null) {
    if (m.index > last) out.push(s.slice(last, m.index))
    const tok = m[0]
    if (tok.startsWith("**")) {
      out.push(
        <strong key={key++} className="font-bold">
          {tok.slice(2, -2)}
        </strong>
      )
    } else if (tok.startsWith("`")) {
      out.push(
        <code
          key={key++}
          className="font-mono text-xs bg-muted px-1 py-0.5"
        >
          {tok.slice(1, -1)}
        </code>
      )
    } else {
      const linkMatch = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(tok)
      if (linkMatch) {
        const text = linkMatch[1]
        const href = linkMatch[2].replace(/^<|>$/g, "")
        out.push(
          <a
            key={key++}
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className="text-primary underline underline-offset-2"
          >
            {text}
          </a>
        )
      } else {
        out.push(tok)
      }
    }
    last = m.index + tok.length
  }
  if (last < s.length) out.push(s.slice(last))
  return out
}

function Markdown({ source }: { source: string }) {
  const blocks = React.useMemo(() => parseMarkdown(source), [source])
  return (
    <div className="space-y-3 text-sm leading-relaxed">
      {blocks.map((b, idx) => {
        if (b.kind === "blank") return null
        if (b.kind === "hr") {
          return (
            <div key={idx} className="h-px w-full bg-border my-4" />
          )
        }
        if (b.kind === "h") {
          if (b.level === 1)
            return (
              <h2
                key={idx}
                className="text-xl font-extrabold tracking-tight mt-2 mb-1"
              >
                {renderInline(b.text)}
              </h2>
            )
          if (b.level === 2)
            return (
              <h3
                key={idx}
                className="text-base font-extrabold tracking-tight mt-6 pb-1 border-b border-border"
              >
                {renderInline(b.text)}
              </h3>
            )
          return (
            <h4 key={idx} className="text-sm font-bold mt-4">
              {renderInline(b.text)}
            </h4>
          )
        }
        if (b.kind === "p")
          return (
            <p key={idx} className="text-muted-foreground">
              {renderInline(b.text)}
            </p>
          )
        if (b.kind === "ul")
          return (
            <ul key={idx} className="list-disc pl-5 space-y-1 marker:text-muted-foreground">
              {b.items.map((it, j) => (
                <li key={j} className="text-muted-foreground">
                  {renderInline(it)}
                </li>
              ))}
            </ul>
          )
        if (b.kind === "table")
          return (
            <div key={idx} className="border border-border my-3">
              <table className="w-full text-sm">
                <thead className="bg-muted/30 text-[10px] uppercase tracking-wider text-muted-foreground">
                  <tr>
                    {b.head.map((h, j) => (
                      <th
                        key={j}
                        className="text-left font-semibold px-3 py-2"
                      >
                        {renderInline(h)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {b.rows.map((row, j) => (
                    <tr key={j} className="border-t border-border">
                      {row.map((cell, k) => (
                        <td key={k} className="px-3 py-2">
                          {renderInline(cell)}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        return null
      })}
    </div>
  )
}

// ------------------------------------------------------------------
// SupportTab — the maintainer's side of the conversation.
//
// Threads awaiting a reply sort to the top, because the only question
// this screen has to answer on open is "who is waiting on me". The
// snapshot each message carries is folded away by default: it is there
// when a reply needs it and noise when it doesn't.
// ------------------------------------------------------------------

type AdminSupportMessage = {
  id: string
  kind: string
  body: string
  fromStaff: boolean
  createdAt: string | null
  snapshot?: Record<string, unknown>
}

type AdminSupportThread = {
  userId: string
  username: string
  email: string
  lastAt: string | null
  awaitingReply: boolean
  messages: AdminSupportMessage[]
}

function SupportTab() {
  const [threads, setThreads] = React.useState<AdminSupportThread[] | null>(null)
  const [selected, setSelected] = React.useState<string | null>(null)
  const [reply, setReply] = React.useState("")
  const [sending, setSending] = React.useState(false)
  const [showSnapshot, setShowSnapshot] = React.useState<string | null>(null)

  const load = React.useCallback(() => {
    fetch("/api/support/admin/threads", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (!d) return
        const list = (d.threads as AdminSupportThread[]).slice().sort((a, b) => {
          if (a.awaitingReply !== b.awaitingReply) return a.awaitingReply ? -1 : 1
          return (b.lastAt || "").localeCompare(a.lastAt || "")
        })
        setThreads(list)
        setSelected((cur) => cur ?? list[0]?.userId ?? null)
      })
      .catch(() => setThreads([]))
  }, [])

  React.useEffect(() => {
    load()
  }, [load])

  const thread = threads?.find((t) => t.userId === selected) ?? null

  const send = async () => {
    const text = reply.trim()
    if (!text || !thread || sending) return
    setSending(true)
    try {
      const res = await fetch(
        `/api/support/admin/threads/${encodeURIComponent(thread.userId)}/reply`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ body: text }),
        }
      ).catch(() => null)
      if (!res || !res.ok) return
      setReply("")
      load()
    } finally {
      setSending(false)
    }
  }

  if (threads === null)
    return <div className="text-sm text-muted-foreground">Loading…</div>
  if (threads.length === 0)
    return (
      <div className="text-sm text-muted-foreground">
        Nobody has written in yet.
      </div>
    )

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[260px_1fr] gap-6">
      <div className="space-y-2">
        {threads.map((t) => (
          <button
            key={t.userId}
            type="button"
            onClick={() => setSelected(t.userId)}
            className={cn(
              "w-full text-left border p-3 cursor-pointer",
              t.userId === selected ? "border-white" : "border-border"
            )}
          >
            <div className="flex items-center gap-2">
              {t.awaitingReply && (
                <span className="inline-block size-2 rounded-full bg-amber-500 shrink-0" />
              )}
              <span className="text-sm font-semibold truncate">{t.username}</span>
            </div>
            <div className="text-[11px] text-muted-foreground font-mono truncate">
              {t.lastAt ? formatRelativeDate(t.lastAt) : ""}
            </div>
          </button>
        ))}
      </div>

      {thread && (
        <div>
          <div className="text-xs text-muted-foreground font-mono mb-3">
            {thread.email}
          </div>
          <div className="space-y-2">
            {thread.messages.map((m) => (
              <div
                key={m.id}
                className={cn(
                  "p-4 border",
                  m.fromStaff ? "border-white" : "border-border"
                )}
              >
                <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-2">
                  {m.fromStaff ? "You" : `${thread.username} · ${m.kind}`}
                  {m.createdAt && (
                    <span className="ml-2 opacity-60 font-mono normal-case tracking-normal">
                      {formatRelativeDate(m.createdAt)}
                    </span>
                  )}
                </div>
                <p className="text-sm leading-relaxed whitespace-pre-wrap break-words">
                  {m.body}
                </p>
                {m.snapshot && (
                  <>
                    <button
                      type="button"
                      onClick={() =>
                        setShowSnapshot(showSnapshot === m.id ? null : m.id)
                      }
                      className="mt-3 text-[11px] text-muted-foreground font-semibold cursor-pointer"
                    >
                      {showSnapshot === m.id ? "Hide" : "Show"} account state
                    </button>
                    {showSnapshot === m.id && (
                      <pre className="mt-2 p-3 border border-border text-[11px] font-mono whitespace-pre-wrap break-words overflow-x-auto">
                        {JSON.stringify(m.snapshot, null, 2)}
                      </pre>
                    )}
                  </>
                )}
              </div>
            ))}
          </div>

          <div className="mt-4 border border-border p-4">
            <textarea
              value={reply}
              onChange={(e) => setReply(e.target.value)}
              rows={4}
              placeholder={`Reply to ${thread.username}…`}
              className="w-full border border-border bg-transparent p-3 text-sm text-foreground outline-none placeholder:text-muted-foreground focus:border-white focus:bg-white/5 resize-y"
            />
            <div className="flex items-center justify-between gap-4 mt-3">
              <p className="text-xs text-muted-foreground">
                Sent to them by email and shown on their Support page.
              </p>
              <Button onClick={() => void send()} disabled={!reply.trim() || sending}>
                {sending ? "Sending…" : "Send reply"}
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
