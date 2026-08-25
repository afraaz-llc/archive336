/**
 * Client-side error capture. Posts uncaught errors and unhandled
 * promise rejections to /api/errors so the admin /dev page can show
 * them. Best-effort - if the report itself fails, we don't try to
 * recover (would risk infinite loops).
 *
 * Wire-up: call installErrorReporter() once at app startup (main.tsx).
 */

let installed = false

async function postError(payload: {
  message: string
  stack?: string
  requestPath?: string
}): Promise<void> {
  try {
    await fetch("/api/errors", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      // Keep it short - if it doesn't go through quickly, drop it
      // rather than stack up retries that could pile on whatever's
      // already wrong.
      keepalive: true,
    })
  } catch {
    // Swallow - reporter must never throw or it'll create a loop.
  }
}

export function reportError(error: unknown, context?: { requestPath?: string }) {
  let message: string
  let stack: string | undefined
  if (error instanceof Error) {
    message = error.message || error.name || "Error"
    stack = error.stack
  } else if (typeof error === "string") {
    message = error
  } else {
    try {
      message = JSON.stringify(error)
    } catch {
      message = String(error)
    }
  }
  void postError({
    message: message.slice(0, 4000),
    stack: stack ? stack.slice(0, 20000) : undefined,
    requestPath:
      context?.requestPath ??
      (typeof window !== "undefined" ? window.location.pathname : undefined),
  })
}

export function installErrorReporter(): void {
  if (installed || typeof window === "undefined") return
  installed = true

  window.addEventListener("error", (event: ErrorEvent) => {
    // event.error is sometimes null (cross-origin script errors). Fall
    // back to event.message in that case so we still capture something.
    reportError(event.error ?? event.message ?? "window.error")
  })

  window.addEventListener(
    "unhandledrejection",
    (event: PromiseRejectionEvent) => {
      reportError(event.reason ?? "unhandledrejection")
    }
  )
}
