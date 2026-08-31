import * as React from "react"
import { useLocation, useSearchParams } from "react-router-dom"
import {
  Settings as SettingsIcon,
  MonitorDown,
  User as UserIcon,
  CreditCard,
  Download,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { AccountEditor, AccountDetails } from "@/components/AccountEditor"
import { DangerZone } from "@/components/DangerZone"
import { SessionsPanel } from "@/components/SessionsPanel"
import { CurrentSessionCard } from "@/components/CurrentSessionCard"
import { AccountSwitcher } from "@/components/AccountSwitcher"
import { PlanCard } from "@/components/billing/PlanCard"
import { PlanPicker } from "@/components/billing/PlanPicker"
import { PaymentMethodSection } from "@/components/billing/PaymentMethodSection"
import { BillingHistory } from "@/components/billing/BillingHistory"
import { CancelPlanSection } from "@/components/billing/CancelPlanSection"
import { Paywalled } from "@/components/Paywalled"
import { SetupAlert } from "@/components/SetupAlert"
import { SidebarCustomizer } from "@/components/SidebarCustomizer"
import { AppearanceToggle } from "@/components/AppearanceToggle"
import {
  BILLING_CHANGE_EVENT,
  hasKnownPaymentStatus,
  readEffectivePaymentStatus,
  readHasPaymentMethod,
  refreshBillingStatus,
} from "@/lib/paymentStatus"
import { useDocumentTitle } from "@/lib/useDocumentTitle"
import { detectWorkerOS, type WorkerOS } from "@/lib/platform"


type SettingsTab = "general" | "worker" | "account" | "billing"

const SETTINGS_TABS: { id: SettingsTab; label: string; icon: React.ReactNode }[] =
  [
    { id: "general", label: "General", icon: <SettingsIcon className="size-4" /> },
    { id: "worker", label: "Worker App", icon: <MonitorDown className="size-4" /> },
    { id: "account", label: "Account", icon: <UserIcon className="size-4" /> },
    { id: "billing", label: "Billing", icon: <CreditCard className="size-4" /> },
  ]

// Connections used to be its own tab. It isn't: every connection is
// established and revoked inside the worker app, so the card could only
// ever say "Managed in your worker app" while sitting on a page that
// implied otherwise. It now lives on the Worker App tab next to the
// download that produces it.
//
// Old ?tab=connections links (bookmarks, anything already sent out) keep
// resolving rather than silently falling back to General.
const TAB_ALIASES: Record<string, SettingsTab> = {
  connections: "worker",
}

// Deep-links point at sections that now live inside a non-default tab:
//   #connections -> Worker App tab (OAuth-disconnected email)
//   #payment     -> Billing tab (Paywalled CTA, signup redirect, the
//                   YouTube 402 redirect)
// Map the hash to its owning tab so the scroll effect can activate that
// tab before it tries to scroll - the target element isn't mounted
// until its tab is rendered. Hashes not listed here resolve within the
// General tab (the default).
const HASH_TO_TAB: Record<string, SettingsTab> = {
  payment: "billing",
  connections: "worker",
}

export default function Settings() {
  useDocumentTitle("Settings")
  const [searchParams, setSearchParams] = useSearchParams()

  // Active tab lives in a ?tab= query param (not the URL hash) so the
  // existing #payment / #connections email-deep-link scroll behavior
  // keeps working untouched. Default "general".
  const rawTab = searchParams.get("tab")
  const tab: SettingsTab = SETTINGS_TABS.some((t) => t.id === rawTab)
    ? (rawTab as SettingsTab)
    : (rawTab && TAB_ALIASES[rawTab]) || "general"
  const setTab = (next: SettingsTab) => {
    const params = new URLSearchParams(searchParams)
    if (next === "general") params.delete("tab")
    else params.set("tab", next)
    setSearchParams(params, { replace: true })
  }

  const [paymentStatus, setPaymentStatus] = React.useState<string | null>(
    () => readEffectivePaymentStatus()
  )
  // Card on file - decoupled from being on a plan. Drives the "no payment
  // method" indicators (alert, red Billing tab); paymentStatus still
  // drives the on-a-plan "Usage & billing" gating + the premium paywalls.
  const [hasCard, setHasCard] = React.useState<boolean>(() =>
    readHasPaymentMethod()
  )
  // Whether billing has been checked yet — gates the "no payment method"
  // indicators (alert + red Billing tab) so they don't flash before the
  // real status is known. We warm the cache on mount too, so a cold
  // deep-link straight to Settings resolves correctly instead of hiding
  // the indicators forever.
  const [billingKnown, setBillingKnown] = React.useState<boolean>(() =>
    hasKnownPaymentStatus()
  )

  // Stay in sync with PlanCard's cache (and the dev override) so the
  // indicators update the moment the user adds a card without a refresh.
  React.useEffect(() => {
    const sync = () => {
      setPaymentStatus(readEffectivePaymentStatus())
      setHasCard(readHasPaymentMethod())
      setBillingKnown(hasKnownPaymentStatus())
    }
    window.addEventListener("storage", sync)
    window.addEventListener(BILLING_CHANGE_EVENT, sync)
    void refreshBillingStatus()
    return () => {
      window.removeEventListener("storage", sync)
      window.removeEventListener(BILLING_CHANGE_EVENT, sync)
    }
  }, [])

  // Any /settings#<id> hash where <id> matches a real section element
  // gets vertically centered in the viewport instead of pinned to the
  // top (the browser default). Used by transactional emails so the
  // user lands directly on the section the email is about - payment
  // on #payment, OAuth connections on #connections, etc. Re-runs on
  // hash change so back/forward navigation still centers.
  const location = useLocation()
  React.useEffect(() => {
    const hash = location.hash.replace(/^#/, "")
    if (!hash) return
    // Some deep-link targets now live inside a non-default tab (e.g.
    // #connections -> Worker App tab). Switch to that tab first so the
    // target element is actually mounted before we scroll. Setting the
    // tab re-runs this effect (it depends on `tab`), and the second
    // pass falls through to the scroll below.
    const tabForHash = HASH_TO_TAB[hash]
    if (tabForHash && tabForHash !== tab) {
      setSearchParams(
        (prev) => {
          const p = new URLSearchParams(prev)
          if (tabForHash === "general") p.delete("tab")
          else p.set("tab", tabForHash)
          return p
        },
        { replace: true }
      )
      return
    }
    // rAF lets sections that fetch on mount (PlanCard, YouTubeSection)
    // finish laying out before we measure where to scroll to.
    const id = window.requestAnimationFrame(() => {
      const el = document.getElementById(hash)
      // Instant scroll, not smooth - the rest of the site uses snap
      // state changes; a tween here would be visually inconsistent.
      if (el) el.scrollIntoView({ behavior: "auto", block: "center" })
    })
    return () => window.cancelAnimationFrame(id)
  }, [location.hash, tab, setSearchParams])

  return (
    <div className="p-8 max-w-4xl mx-auto">
      <h1 className="text-2xl font-extrabold tracking-tight">Settings</h1>

      <div className="mt-6 border-b border-border flex gap-0 overflow-x-auto overflow-y-hidden">
        {SETTINGS_TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setTab(t.id)}
            className={
              "flex items-center gap-2 px-4 py-2.5 text-sm font-semibold cursor-pointer border-b-2 -mb-px whitespace-nowrap " +
              (tab === t.id
                ? "border-foreground text-foreground"
                : t.id === "billing" && billingKnown && !hasCard
                ? "border-transparent text-destructive"
                : "border-transparent text-muted-foreground hover:text-foreground")
            }
          >
            {t.icon}
            {t.label}
          </button>
        ))}
      </div>

      {/* General tab: account-setup alerts (shown only while unresolved)
          plus left-sidebar layout customization. */}
      {tab === "general" && (
        <div className="mt-8 space-y-8">
          {billingKnown && !hasCard && (
            <SetupAlert
              icon={<CreditCard className="size-4" />}
              title="Add a payment method"
              to="/settings#payment"
            />
          )}
          <SidebarCustomizer />
          <AppearanceToggle />
        </div>
      )}

      {/* Account tab: credentials (Authentication), read-only metadata
          (Details), active logins (Sessions), the current-session + Log
          out card, and Compliance (export / delete / legal) at the
          bottom. Moved out of the General tab. */}
      {tab === "account" && (
        <div className="mt-8 space-y-8">
          <section>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">
              Accounts
            </div>
            {/* Never paywalled: switching between your own accounts is
                navigation, not a premium feature — and gating it would trap
                a card-less account (you couldn't switch back to a carded
                one). */}
            <AccountSwitcher />
          </section>

          <section>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">
              Authentication
            </div>
            <AccountEditor />
          </section>

          <section>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">
              Details
            </div>
            <AccountDetails />
          </section>

          <section>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">
              Sessions
            </div>
            {/* Never paywalled: reviewing + signing out your other logins
                is account security, not a premium feature — same reasoning
                as AccountSwitcher above and CurrentSessionCard below. */}
            <SessionsPanel />
          </section>

          {/* Current-session summary + Log out. Same logout action as
              the sidebar button; kept outside any paywall so it always
              works, even before payment is set up. */}
          <CurrentSessionCard />

          <section>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">
              Compliance
            </div>
            <DangerZone />
          </section>
        </div>
      )}

      {/* Billing tab: plan + payment method. The #payment deep-link
          (HASH_TO_TAB) activates this tab and the id below is its
          scroll target. */}
      {tab === "billing" && (
        <div className="mt-8 space-y-8">
          {/* Plans picker - choose or switch your plan. Locked behind the
              paywall until a payment method is on file: same billingKnown +
              hasCard signal that turns the Manage box red below, so the flow
              is "add a card (red prompt) -> plans unlock". */}
          <section>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">
              Plans
            </div>
            {billingKnown && !hasCard ? (
              <Paywalled iconOnly>
                <PlanPicker />
              </Paywalled>
            ) : (
              <PlanPicker />
            )}
          </section>
          {/* Usage + the manage-card button only matter once a plan is
              active - hidden until then so we don't show a bill to
              someone who hasn't picked a plan. */}
          {(paymentStatus === "active" || paymentStatus === "past_due") && (
            <section>
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">
                Usage &amp; billing
              </div>
              <PlanCard />
            </section>
          )}
          {/* Billing history — receipts. Never paywalled: people always
              need access to their own invoices. Empty until first bill. */}
          <section>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">
              Invoices
            </div>
            <BillingHistory />
          </section>

          {/* Manage: payment method + cancel (+ future billing actions),
              grouped in one bordered container with a header — mirrors the
              Account tab's Compliance section. The #payment deep-link
              (Add-payment CTAs) lands here. Cancel row only on an active
              plan. */}
          <section>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">
              Manage
            </div>
            <div
              id="payment"
              className={`border divide-y divide-border ${
                billingKnown && !hasCard
                  ? "border-destructive"
                  : "border-border"
              }`}
            >
              <PaymentMethodSection alert={billingKnown && !hasCard} />
              {paymentStatus === "active" && <CancelPlanSection />}
            </div>
          </section>
        </div>
      )}

      {/* Worker App tab. Connections live here rather than on a tab of
          their own: they are created and revoked inside the worker app,
          so they belong beside the download that installs it. The
          #connections email deep-link (HASH_TO_TAB) activates this tab
          and the id below is its scroll target. */}
      {/* The YouTube connect/import card used to live here. It was the
          old account-level flow: sign in to a Google account, then import
          whatever channels it owned. Authentication is per-channel now and
          happens in the worker app, driven by the channels tracked on the
          website - so this card offered a second, contradictory way in.
          Per-channel status and disconnect live on the channel's own
          settings panel; tracking starts from the YouTube page. */}
      {tab === "worker" && (
        <div className="mt-8 space-y-8">
          <WorkerAppSection />
        </div>
      )}

    </div>
  )
}

// ---- Worker App download section --------------------------------------
// Per-OS download buttons for the desktop worker app. Installers aren't
// published yet (the app needs code-signing first), so each entry's `url` is
// null and renders a "Coming soon" state. Flip a url on once the signed
// installer for that platform is hosted and that button goes live.


const WORKER_DOWNLOADS: {
  os: WorkerOS
  label: string
  detail: string
  url: string | null
}[] = [
  { os: "macos", label: "macOS", detail: "Apple Silicon & Intel · .dmg", url: null },
  { os: "windows", label: "Windows", detail: "Windows 10 / 11 · .msi", url: null },
  { os: "linux", label: "Linux", detail: "AppImage", url: null },
]

function WorkerAppSection() {
  const detected = React.useMemo(detectWorkerOS, [])
  return (
    <div className="mt-8 space-y-8">
      <section>
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">
          Download
        </div>
        <div className="space-y-3 max-w-md">
          {WORKER_DOWNLOADS.map((d) => {
            const isDetected = d.os === detected
            return (
              <div
                key={d.os}
                className={`flex items-center justify-between gap-4 border p-4 ${
                  isDetected ? "border-foreground" : "border-border"
                }`}
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm font-semibold">{d.label}</span>
                    {isDetected && (
                      <span className="text-[10px] uppercase tracking-wider font-bold border border-foreground/40 px-1.5 py-0.5">
                        Your system
                      </span>
                    )}
                  </div>
                  <div className="text-xs text-muted-foreground mt-0.5">
                    {d.detail}
                  </div>
                </div>
                {d.url ? (
                  <Button
                    asChild
                    size="sm"
                    variant={isDetected ? "default" : "outline"}
                  >
                    <a href={d.url} download>
                      <Download className="size-4" />
                      Download
                    </a>
                  </Button>
                ) : (
                  <span className="text-xs text-muted-foreground whitespace-nowrap">
                    Coming soon
                  </span>
                )}
              </div>
            )
          })}
        </div>
      </section>
    </div>
  )
}
