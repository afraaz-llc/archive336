import { Suspense } from "react"
import { lazyWithReload } from "@/lib/lazyWithReload"
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom"
import { AuthProvider } from "@/auth/AuthContext"
import { PricingProvider } from "@/lib/pricing"
import RequireAuth from "@/components/RequireAuth"
// Eager: the two things a first-time visitor hits immediately. Keeping these
// in the initial chunk means the landing paints without waiting on the rest
// of the app to download.
import Auth from "@/pages/Auth"

// Everything else is code-split into its own chunk and loaded on demand, so a
// logged-out visitor never downloads the whole app (settings, admin, the
// YouTube pages, etc.) just to see the landing. (LandingPage stays eager via
// RequireAuth's direct import — it's the anon "/" view.)
const AppShell = lazyWithReload(() => import("@/components/AppShell"))
const Home = lazyWithReload(() => import("@/pages/Home"))
const YouTube = lazyWithReload(() => import("@/pages/YouTube"))
const ChannelDetail = lazyWithReload(() => import("@/pages/ChannelDetail"))
const ChannelComments = lazyWithReload(() => import("@/pages/ChannelComments"))
const Settings = lazyWithReload(() => import("@/pages/Settings"))
const Admin = lazyWithReload(() => import("@/pages/Admin"))
const Dev = lazyWithReload(() => import("@/pages/Dev"))
const NotFound = lazyWithReload(() => import("@/pages/NotFound"))
const ForgotPassword = lazyWithReload(() => import("@/pages/ForgotPassword"))
const ResetPassword = lazyWithReload(() => import("@/pages/ResetPassword"))
const VerifyEmail = lazyWithReload(() => import("@/pages/VerifyEmail"))
const ConfirmDelete = lazyWithReload(() => import("@/pages/ConfirmDelete"))
const AccountDeleted = lazyWithReload(() => import("@/pages/AccountDeleted"))
const Terms = lazyWithReload(() => import("@/pages/legal/Terms"))
const Privacy = lazyWithReload(() => import("@/pages/legal/Privacy"))

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <PricingProvider>
          {/* Pitch-black fallback (matches the app bg) while a lazy chunk
              loads — no white flash, no spinner. */}
          <Suspense fallback={<div className="min-h-screen bg-background" />}>
            <Routes>
              {/* Public pages — auth flows and the legal docs anyone needs
                  to be able to read before (or without) signing up. */}
              <Route path="/auth" element={<Auth />} />
              <Route path="/forgot-password" element={<ForgotPassword />} />
              <Route path="/reset-password" element={<ResetPassword />} />
              <Route path="/verify-email" element={<VerifyEmail />} />
              <Route path="/confirm-delete" element={<ConfirmDelete />} />
              <Route path="/account-deleted" element={<AccountDeleted />} />
              <Route path="/terms" element={<Terms />} />
              <Route path="/privacy" element={<Privacy />} />

              <Route element={<RequireAuth />}>
                <Route element={<AppShell />}>
                  <Route path="/" element={<Home />} />
                  <Route path="/youtube" element={<YouTube />} />
                  <Route path="/youtube/channel/:channelId" element={<ChannelDetail />} />
                  <Route path="/youtube/channel/:channelId/comments" element={<ChannelComments />} />
                  {/* Support moved into Settings. Kept as a redirect
                      rather than deleted: every support reply email
                      already sent points here, and those land in
                      someone's inbox long after the nav changed. */}
                  <Route
                    path="/support"
                    element={<Navigate to="/settings?tab=support" replace />}
                  />
                  <Route path="/settings" element={<Settings />} />
                  <Route path="/admin" element={<Admin />} />
                  <Route path="/dev" element={<Dev />} />
                  <Route path="/404" element={<NotFound />} />
                  <Route path="*" element={<Navigate to="/404" replace />} />
                </Route>
              </Route>
            </Routes>
          </Suspense>
        </PricingProvider>
      </AuthProvider>
    </BrowserRouter>
  )
}
