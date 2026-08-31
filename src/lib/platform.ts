/**
 * Which desktop platform the visitor is on.
 *
 * Shared because two places need the same answer for related reasons:
 * Settings picks which download to highlight, and the landing page has
 * to tell a Windows or Linux visitor that the app they are about to pay
 * for does not run on their machine yet. Two copies of this could drift
 * into disagreeing about who gets warned, which is the one thing the
 * warning cannot afford to be wrong about.
 */
export type WorkerOS = "macos" | "windows" | "linux"

export function detectWorkerOS(): WorkerOS | null {
  if (typeof navigator === "undefined") return null
  const ua = navigator.userAgent
  const platform = navigator.platform || ""
  if (/Mac/i.test(platform) || /Mac OS X/i.test(ua)) return "macos"
  if (/Win/i.test(platform) || /Windows/i.test(ua)) return "windows"
  if (/Linux/i.test(platform) && !/Android/i.test(ua)) return "linux"
  return null
}

/** Platforms with a shipping build today. macOS only at launch. */
export const SUPPORTED_PLATFORMS: WorkerOS[] = ["macos"]

export const PLATFORM_LABELS: Record<WorkerOS, string> = {
  macos: "macOS",
  windows: "Windows",
  linux: "Linux",
}
