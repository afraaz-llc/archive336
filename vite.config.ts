import { defineConfig, type Plugin } from "vite"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"
import { fileURLToPath, URL } from "node:url"

// Inject <link rel="preload"> for the above-the-fold font weights so a
// first-time (uncached) visitor paints in the brand font instead of a
// fallback. Fonts are content-hashed into /assets at build time, so we read
// the emitted filenames out of the bundle rather than hardcoding hashes.
// Only the weights visible in the landing hero are preloaded — preloading
// every weight would waste bandwidth on glyphs not shown at first paint.
function preloadFonts(): Plugin {
  const critical = [
    "nunito-latin-800-normal", // headline + wordmark + CTA
    "nunito-latin-600-normal", // hero subtext
    "jetbrains-mono-latin-400-normal", // mono labels + footer
  ]
  return {
    name: "preload-critical-fonts",
    apply: "build",
    enforce: "post",
    transformIndexHtml(html, ctx) {
      if (!ctx.bundle) return html
      const files = Object.keys(ctx.bundle)
      const tags = critical
        .map((base) => files.find((f) => f.includes(base) && f.endsWith(".woff2")))
        .filter((f): f is string => Boolean(f))
        .map((file) => ({
          tag: "link",
          attrs: {
            rel: "preload",
            as: "font",
            type: "font/woff2",
            href: "/" + file,
            crossorigin: "",
          },
          injectTo: "head-prepend" as const,
        }))
      return { html, tags }
    },
  }
}

// Where `npm run dev` proxies /api/* to. Default = production
// backend so the local dev server is one command (`npm run dev`)
// and edits in src/ hot-reload against real prod data.
//
// Override with VITE_DEV_API_TARGET=http://127.0.0.1:8000 in a
// .env.local file to point at a local uvicorn instead (safer if
// you're testing changes that mutate billing state).
const API_TARGET =
  process.env.VITE_DEV_API_TARGET || "https://archive336.com"


// Sentry's browser DSN, inlined at build time.
//
// This is a public identifier by design - it ships in the JS bundle
// whatever we do, and Sentry documents it as safe to expose. It used to
// live in a file called `.env.production`, which was accurate and
// alarming: a public repo with a file by that name invites "you
// committed your secrets" from people who will not open it. The value
// has not changed; only the place that is obviously not a secret store.
const SENTRY_DSN =
  "https://075051b329c9cea4a223838c2c87ba67@o4511422857674752.ingest.us.sentry.io/4511422888869888"

export default defineConfig({
  define: {
    "import.meta.env.VITE_SENTRY_DSN": JSON.stringify(SENTRY_DSN),
  },
  plugins: [react(), tailwindcss(), preloadFonts()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 8787,
    open: false,
    proxy: {
      "/api": {
        target: API_TARGET,
        changeOrigin: true,
        // Trust prod's cert (the default does this, but be explicit).
        secure: true,
        // Auth cookies from prod come back with Domain=aetherarchivetool
        // .com, which the browser won't store for a localhost page.
        // Rewrite the cookie domain so the browser keeps the session
        // alive across the proxy hop. (Chrome treats localhost as
        // Secure-allowed so the Secure attribute stays compatible.)
        cookieDomainRewrite: "localhost",
      },
    },
  },
})
