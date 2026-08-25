import { Navigate, Outlet, useLocation } from "react-router-dom"
import { useAuth } from "@/auth/AuthContext"
import LandingPage from "@/pages/LandingPage"

export default function RequireAuth() {
  const { state } = useAuth()
  const location = useLocation()

  if (state.status === "loading") {
    // Blank pitch-black screen during the initial /me probe — matches the app bg,
    // avoids a "flash of unauthenticated content".
    return <div className="min-h-screen bg-background" />
  }

  if (state.status === "anon") {
    // Logged-out visitors get the public landing page at the root, and are
    // sent to the auth screen for every other (deep-linked) protected path.
    if (location.pathname === "/") {
      return <LandingPage />
    }
    return <Navigate to="/auth" replace />
  }

  return <Outlet />
}
