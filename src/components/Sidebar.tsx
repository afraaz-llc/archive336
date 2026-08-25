import * as React from "react"
import { Link, NavLink } from "react-router-dom"
import {
  ChevronLeft,
  FlaskConical,
  LogOut,
  MessageSquare,
  MonitorPlay,
  Settings,
  Shield,
} from "lucide-react"
import { cn } from "@/lib/utils"
import { useAuth } from "@/auth/AuthContext"
import { Logo } from "@/components/Logo"
import {
  BILLING_CHANGE_EVENT,
  readHasPaymentMethod,
} from "@/lib/paymentStatus"
import {
  SIDEBAR_PREFS_EVENT,
  labelForSidebarItem,
  readSidebarPrefs,
  type SidebarItemId,
  type SidebarPref,
} from "@/lib/sidebarPrefs"
import {
  SIDEBAR_COLLAPSED_EVENT,
  readSidebarCollapsed,
  setSidebarCollapsed,
} from "@/lib/uiPrefs"

// Visual + routing details per customizable nav item. Order and on/off
// come from sidebar prefs; this just maps an id to how it renders.
// Admin/Dev are handled separately below (is_admin-gated, not reorderable).
const NAV_REGISTRY: Record<SidebarItemId, { to: string; icon: React.ReactNode }> =
  {
    home: { to: "/", icon: <Logo className="size-5" /> },
    youtube: { to: "/youtube", icon: <MonitorPlay className="size-5" /> },
    support: { to: "/support", icon: <MessageSquare className="size-5" /> },
  }

export function Sidebar() {
  const { state, logout } = useAuth()
  const user = state.status === "authed" ? state.user : null

  const [collapsed, setCollapsed] = React.useState<boolean>(() =>
    readSidebarCollapsed()
  )

  // Track payment status so the Settings icon can flag "needs attention"
  // when the user isn't on a healthy paying state. Listen for both
  // cross-tab storage events and a same-tab custom event that PlanCard
  // dispatches when /api/billing/status returns fresh data.
  const [hasCard, setHasCard] = React.useState<boolean>(() =>
    readHasPaymentMethod()
  )

  // User-chosen nav layout (which items show + their order), edited from
  // the General settings tab and broadcast back here via SIDEBAR_PREFS_EVENT.
  const [sidebarPrefs, setSidebarPrefs] = React.useState<SidebarPref[]>(() =>
    readSidebarPrefs()
  )

  // Collapsed state is owned by uiPrefs (localStorage cache + account
  // sync). Re-read when it changes - e.g. hydrated from the account on
  // login, so a collapsed sidebar follows the user to a new device.
  React.useEffect(() => {
    const sync = () => setCollapsed(readSidebarCollapsed())
    window.addEventListener(SIDEBAR_COLLAPSED_EVENT, sync)
    window.addEventListener("storage", sync)
    return () => {
      window.removeEventListener(SIDEBAR_COLLAPSED_EVENT, sync)
      window.removeEventListener("storage", sync)
    }
  }, [])

  React.useEffect(() => {
    const sync = () => setSidebarPrefs(readSidebarPrefs())
    window.addEventListener(SIDEBAR_PREFS_EVENT, sync)
    window.addEventListener("storage", sync)
    return () => {
      window.removeEventListener(SIDEBAR_PREFS_EVENT, sync)
      window.removeEventListener("storage", sync)
    }
  }, [])

  React.useEffect(() => {
    const sync = () => setHasCard(readHasPaymentMethod())
    window.addEventListener("storage", sync)
    window.addEventListener(BILLING_CHANGE_EVENT, sync)
    return () => {
      window.removeEventListener("storage", sync)
      window.removeEventListener(BILLING_CHANGE_EVENT, sync)
    }
  }, [])

  const toggleCollapsed = () => setSidebarCollapsed(!collapsed)

  // Settings icon goes red whenever the user has something unresolved
  // that lives behind /settings — unverified email, or no card on file.
  // The active NavLink styling (when on /settings) overrides this so the
  // icon is just normally-active when they're already there.
  const needsAttention =
    (user !== null && !user.email_verified) || !hasCard

  return (
    <aside
      className={cn(
        "shrink-0 border-r border-border bg-card/30 h-screen sticky top-0 flex flex-col",
        collapsed ? "w-14" : "w-[200px]"
      )}
    >
      {/* Brand row — collapse toggle lives here */}
      <div className="border-b border-border p-2 flex items-center gap-2.5">
        <button
          type="button"
          onClick={toggleCollapsed}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          className="size-10 bg-primary/15 border border-primary/30 flex items-center justify-center cursor-pointer shrink-0"
        >
          <ChevronLeft
            className={cn("size-5 text-primary", collapsed && "rotate-180")}
          />
        </button>
        {!collapsed && (
          <Link
            to="/"
            className="text-sm font-extrabold leading-tight truncate"
          >
            ARCHIVE336
          </Link>
        )}
      </div>

      {/* Nav */}
      <nav className="p-2 flex-1 space-y-1">
        {sidebarPrefs
          .filter((p) => p.enabled)
          .map((p) => (
            <NavItem
              key={p.id}
              to={NAV_REGISTRY[p.id].to}
              icon={NAV_REGISTRY[p.id].icon}
              label={labelForSidebarItem(p.id)}
              collapsed={collapsed}
            />
          ))}
        {user?.is_admin && (
          <NavItem
            to="/admin"
            icon={<Shield className="size-5" />}
            label="Admin"
            collapsed={collapsed}
          />
        )}
        {user?.is_admin && (
          <NavItem
            to="/dev"
            icon={<FlaskConical className="size-5" />}
            label="Dev"
            collapsed={collapsed}
          />
        )}
      </nav>

      {/* User footer */}
      {user && (
        <div className="border-t border-border group flex items-center p-2 gap-2">
          <NavLink
            to="/settings"
            aria-label={needsAttention ? "Settings — needs attention" : "Settings"}
            className={({ isActive }) =>
              cn(
                "size-10 flex items-center justify-center cursor-pointer border shrink-0",
                isActive
                  ? "bg-accent border-white text-foreground"
                  : needsAttention
                  ? "border-destructive text-destructive"
                  : "border-border text-muted-foreground"
              )
            }
          >
            <Settings className="size-5" />
          </NavLink>
          {!collapsed && (
            <>
              <div className="min-w-0 flex-1 text-sm font-semibold truncate">
                {user.username}
              </div>
              <button
                type="button"
                onClick={() => void logout()}
                aria-label="Log out"
                className="size-10 flex items-center justify-center text-destructive opacity-0 group-hover:opacity-100 cursor-pointer shrink-0"
              >
                <LogOut className="size-5 -scale-x-100" />
              </button>
            </>
          )}
        </div>
      )}
    </aside>
  )
}

function NavItem({
  to,
  icon,
  label,
  collapsed,
}: {
  to: string
  icon: React.ReactNode
  label: string
  collapsed: boolean
}) {
  return (
    <NavLink
      to={to}
      end
      aria-label={collapsed ? label : undefined}
      className={({ isActive }) =>
        collapsed
          ? cn(
              "size-10 mx-auto flex items-center justify-center border",
              isActive
                ? "bg-accent border-white text-foreground"
                : "border-border text-muted-foreground"
            )
          : cn(
              "flex items-center gap-2.5 px-2.5 py-2.5 text-sm font-medium",
              isActive
                ? "bg-accent text-foreground"
                : "text-muted-foreground"
            )
      }
    >
      {icon}
      {!collapsed && <span>{label}</span>}
    </NavLink>
  )
}

