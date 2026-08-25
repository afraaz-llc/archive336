import * as React from "react"
import { Link } from "react-router-dom"
import { Logo } from "@/components/Logo"

/**
 * Public landing page — the logged-OUT home at "/".
 *
 * A lean, deadpan single hero (pared back from a larger build): pitch black,
 * sharp corners, an uppercase mono micro-label, extrabold headline, the
 * signature white block CTA, a closing CTA, and a mono footer. No motion,
 * transitions, shadows, or effects.
 */
export default function LandingPage() {
  return (
    <div className="min-h-screen flex flex-col bg-background text-foreground">
      {/* ═══ STICKY HEADER ═══ */}
      <header className="sticky top-0 z-20 bg-background border-b border-white/15 flex items-center justify-between px-6 py-3.5">
        <div className="flex items-center gap-2.5">
          <Logo className="w-5 h-5" />
          <span className="text-[15px] font-extrabold tracking-[0.02em]">ARCHIVE336</span>
        </div>
        <Link
          to="/auth#login"
          className="font-mono text-[11px] uppercase tracking-[0.18em] text-foreground inline-flex items-center px-4 py-3 -my-3 -mr-4"
        >
          Log in
        </Link>
      </header>

      {/* ═══ HERO ═══ */}
      <section className="flex-1 flex items-center px-6 sm:px-10 md:px-16 py-24 md:py-32">
        <div className="max-w-5xl">
          <MonoLabel>// Hoard responsibly</MonoLabel>
          <h1 className="mt-5 text-4xl sm:text-5xl md:text-6xl font-extrabold tracking-tight leading-[1.03]">
            Welcome all Preservation Pirates! Make yourselves at home
          </h1>
          <p className="mt-5 text-base text-muted-foreground font-semibold leading-relaxed max-w-[44ch]">
            ARCHIVE336 - Digital Doomsday Bunker
          </p>
          <div className="mt-9 flex items-center gap-6 flex-wrap">
            <Link
              to="/auth#signup"
              className="inline-flex items-center justify-center h-14 px-9 bg-white text-black text-sm font-extrabold tracking-[0.2em] uppercase border-2 border-white active:bg-neutral-300"
            >
              Start archiving
            </Link>
          </div>
        </div>
      </section>

      {/* ═══ FOOTER INDEX ═══ */}
      <footer className="mt-auto border-t border-white/15 px-6 py-6">
        <div className="max-w-5xl mx-auto font-mono text-[10px] uppercase tracking-[0.12em] text-muted-foreground flex flex-wrap items-center gap-x-3 gap-y-1.5">
          <Link to="/terms" className="hover:text-foreground">Terms</Link>
          <span aria-hidden>·</span>
          <Link to="/privacy" className="hover:text-foreground">Privacy</Link>
          <span aria-hidden>·</span>
          <a href="mailto:support@archive336.com" className="hover:text-foreground">support@archive336.com</a>
        </div>
      </footer>
    </div>
  )
}

/* ── shared primitives ────────────────────────────────────────────────── */

function MonoLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="font-mono text-[11px] uppercase tracking-[0.28em] text-muted-foreground">
      {children}
    </div>
  )
}
