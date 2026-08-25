import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import * as Sentry from '@sentry/react'
// Self-hosted brand fonts — same-origin, cached forever, no external
// round-trip. Declared in fonts.css with font-display: optional so the
// text doesn't load-in-then-swap; the critical weights are preloaded via
// the preloadFonts plugin in vite.config.ts.
import './fonts.css'
import './index.css'
import App from './App.tsx'
import { installErrorReporter } from './lib/errorReporter.ts'

// Initialize Sentry only when a DSN is configured. Empty DSN = no-op,
// which means dev / local builds don't ship phantom errors to the
// Sentry project. The DSN is injected at build time via VITE_SENTRY_DSN.
const sentryDsn = import.meta.env.VITE_SENTRY_DSN as string | undefined
if (sentryDsn) {
  Sentry.init({
    dsn: sentryDsn,
    environment: import.meta.env.MODE,
    // Capture 100% of errors. Tracing samples kept low to stay
    // comfortably inside Sentry's free tier (5k errors/month).
    tracesSampleRate: 0,
    // Don't send default PII (IP, cookies) - the /api/errors pipeline
    // logs identified user IDs explicitly with cleaner privacy semantics.
    sendDefaultPii: false,
  })
}

// Capture uncaught errors + unhandled promise rejections to /api/errors
// so the admin /dev page can surface them. Installed before App renders
// so any error during initial mount is captured too. Works alongside
// Sentry - the admin /dev page is for in-product debugging, Sentry is
// for alerting + aggregation across deploys.
//
// Production builds only. A dev server (including the worker app's vite
// dev server) + HMR otherwise stream transient "X is not defined"
// reload artifacts into the prod error log. import.meta.env.PROD is
// false under `vite dev`, true under `vite build` — mirrors the Sentry
// DSN gating above (the DSN lives only in .env.production).
if (import.meta.env.PROD) {
  installErrorReporter()
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
