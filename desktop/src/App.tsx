import { useEffect, useRef, useState } from "react"
import { invoke } from "@tauri-apps/api/core"
import { listen } from "@tauri-apps/api/event"
import { openUrl } from "@tauri-apps/plugin-opener"
import { ExternalLink, MonitorPlay, Plus, RefreshCw } from "lucide-react"
import { SetupChecklist, type SetupReadiness } from "./SetupChecklist"
import { AccountBox } from "@/components/AccountBox"
import { Button } from "@/components/ui/button"
import { Switch } from "@/components/ui/switch"
import { cn } from "@/lib/utils"
import { enable, disable, isEnabled } from "@tauri-apps/plugin-autostart"

type Tab = "status" | "connections" | "settings"

type StoredConfig = {
  base_url: string
  username: string
  password: string
  // Set only when the user deliberately turns launch-at-login off. Optional
  // because configs written before the field existed simply don't have it,
  // and a missing flag means "never said no" - which is what we want.
  autostartDeclined?: boolean
}

// One connected YouTube account's live status (from list_youtube_accounts).
// Each account has its own isolated session in the worker.
type YoutubeAccount = {
  id: string
  connected: boolean
  cookieCount: number
  channelTitle: string | null
  message: string
}

// One channel the user tracks on the website. The worker never invents
// these - see list_tracked_channels in lib.rs for why the website is the
// only source.
type TrackedChannel = {
  youtubeId: string
  title: string
  handle: string
  thumbnailUrl: string
  authenticated: boolean
  revoked: boolean
}

type WorkerStatus = {
  running: boolean
  loggedIn: boolean
  currentJobId: string | null
  currentVideoId: string | null
  currentVideoTitle: string | null
  currentProgress: number
  lastCompletedVideoId: string | null
  lastError: string | null
  completedCount: number
}

const DEFAULT_STATUS: WorkerStatus = {
  running: false,
  loggedIn: false,
  currentJobId: null,
  currentVideoId: null,
  currentVideoTitle: null,
  currentProgress: 0,
  lastCompletedVideoId: null,
  lastError: null,
  completedCount: 0,
}

function App() {
  const [config, setConfig] = useState<StoredConfig | null>(null)
  const [status, setStatus] = useState<WorkerStatus>(DEFAULT_STATUS)
  // We only care whether yt-dlp is installed - the actual path is
  // implementation detail and intentionally not shown to the user.
  const [ytdlpError, setYtdlpError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [tab, setTab] = useState<Tab>("status")
  // Setup readiness reported by the SetupChecklist - gates the Start
  // button. "loading" while a verify probe is running, "incomplete"
  // when something's missing or denied, "ready" when all required
  // rows are green.
  const [setupReady, setSetupReady] = useState<SetupReadiness>("loading")
  // YouTube account connection state. Set by check_youtube_connection
  // on mount + after the user clicks Connect/Recheck. Drives the
  // status pill + Connect button on the Settings YouTube card.
  // Connected YouTube accounts + their live status (from the worker's
  // list_youtube_accounts). Each account has its own isolated session.
  // Only the setter is used. The list itself is no longer rendered -
  // Connections shows channels now - but list_youtube_accounts re-probes
  // every session AND reports the aggregate up to the backend, which is
  // what the website's public-only warning reads. Keep calling it.
  const [, setAccounts] = useState<YoutubeAccount[]>([])
  const [ytBusy, setYtBusy] = useState(false)
  // Channels the website says this account tracks. null error = fine;
  // a non-null error must NOT be rendered as "no channels", since
  // "we couldn't ask" and "you have none" call for opposite actions.
  const [trackedChannels, setTrackedChannels] = useState<TrackedChannel[]>([])
  const [channelsLoading, setChannelsLoading] = useState(true)
  const [channelsError, setChannelsError] = useState<string | null>(null)
  // The channel we opened the sign-in window for. A ref rather than
  // state because the accounts-changed listener is registered once, on
  // mount, and would otherwise close over a value that is always null.
  const pendingAuthRef = useRef<TrackedChannel | null>(null)
  // Channel title while an ownership probe runs. The probe enumerates the
  // uploads playlist twice, which takes seconds on a big channel, and a
  // silent pause there reads as the app having done nothing.
  const [proving, setProving] = useState<string | null>(null)
  // The account slot connect_youtube_account opened for the pending
  // authentication, so the probe reads the right session's cookies.
  const pendingSlotRef = useRef<string | null>(null)
  // Auth tab state
  const [signinError, setSigninError] = useState<string | null>(null)
  const [signinBusy, setSigninBusy] = useState(false)
  // Launch-at-login (autostart) state for the Settings toggle.
  const [autostart, setAutostart] = useState(false)
  const [autostartBusy, setAutostartBusy] = useState(false)
  // Syncing starts on its own and stays running for as long as the app is
  // open - there is no Start button, because a backup tool that waits to be
  // asked is not a backup tool. autoStartedRef guards a single attempt per
  // app run, which is what keeps a deliberate stop stopped: the worker halts
  // itself when YouTube access is revoked, and restarting it on sight would
  // spin against a revocation the user asked for.
  const autoStartedRef = useRef(false)
  // Guards a single auto-sign-in on launch when stored credentials exist.
  const autoSignedInRef = useRef(false)
  // Guards a single launch-at-login registration per app run.
  const autostartSyncedRef = useRef(false)
  // The user's "no", readable synchronously. The registration pass below
  // has awaits in the middle of it, and a click that lands while those are
  // in flight has to win - reading config state there would only see the
  // value captured before the awaits, and we would re-register a login
  // item the user just switched off with nothing left to take it back off.
  const autostartDeclinedRef = useRef(false)

  // Load saved credentials on mount
  useEffect(() => {
    invoke<StoredConfig>("get_credentials")
      .then((c) => {
        setConfig(c)
        // Auto-sign-in on launch, from the credentials we just loaded off
        // disk - NOT reactively off `config`, which the form mutates on
        // every keystroke. Firing here means a stored login signs itself
        // in once at startup, while typing into the sign-in form never
        // triggers a sign-in until the user submits. Guarded so it runs a
        // single time per app run.
        if (c.username && c.password && !autoSignedInRef.current) {
          autoSignedInRef.current = true
          void signIn({ username: c.username, password: c.password })
        }
      })
      .catch(console.error)
    invoke<WorkerStatus>("get_status").then(setStatus).catch(console.error)
    invoke<string>("ytdlp_check")
      .then(() => setYtdlpError(null))
      .catch((e) => setYtdlpError(String(e)))
    invoke<YoutubeAccount[]>("list_youtube_accounts")
      .then((a) => {
        setAccounts(a)
      })
      .catch(() => {})
  }, [])

  // Pull the tracked-channel list from the website.
  //
  // `quiet` skips the loading state, for refreshes the user did not ask
  // for. Without it the automatic re-read on window focus would blank
  // the list to "Checking channels…" every time this app came forward.
  const refreshChannels = (opts?: { quiet?: boolean }) => {
    if (!opts?.quiet) setChannelsLoading(true)
    invoke<TrackedChannel[]>("list_tracked_channels")
      .then((cs) => {
        setTrackedChannels(cs)
        setChannelsError(null)
        // An explicit re-check settles the wrong-account notice too. It
        // otherwise outlives the problem: sign in wrong, close the
        // window, fix it elsewhere, and the warning sits there with
        // nothing left to warn about.
        const want = pendingAuthRef.current
        if (want && cs.find((c) => c.youtubeId === want.youtubeId)?.authenticated) {
          pendingAuthRef.current = null
        }
      })
      .catch((e) => setChannelsError(String(e)))
      .finally(() => setChannelsLoading(false))
  }

  useEffect(() => {
    refreshChannels()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Re-read after signing in: before that the request is refused, so the
  // first load of a fresh install would otherwise sit on an error until
  // the user found the Refresh button.
  useEffect(() => {
    if (status.loggedIn) refreshChannels()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status.loggedIn])

  // The channel list is owned by the website, so it goes stale the moment
  // a channel is added there - which is the only way to add one. Until
  // now it was re-read on launch and on sign-in and never again, so a
  // channel added while this app was open simply did not exist here: the
  // owner added a second channel, came back, saw one card, and asked
  // whether authenticating more than one was possible at all.
  //
  // Focus is the right trigger because bringing this window forward IS
  // the gesture of "I just did something over there". Quiet, so the list
  // does not blink to "Checking channels…" every time the app comes up.
  useEffect(() => {
    const onFocus = () => {
      if (status.loggedIn) refreshChannels({ quiet: true })
    }
    window.addEventListener("focus", onFocus)
    return () => window.removeEventListener("focus", onFocus)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status.loggedIn])

  // Authenticating is still a Google sign-in, but it is now anchored to a
  // channel the website already tracks rather than to whatever account the
  // user happens to pick. The sign-in window is the same; what changed is
  // that the result has somewhere to land.
  // Opens the sign-in window and stops there.
  //
  // It used to check ownership on the next line, which could not work:
  // connect_youtube_account resolves as soon as the window is BUILT, not
  // when anyone signs in, so the check ran within a second of the window
  // appearing and always found the channel unauthenticated - because it
  // was, the user hadn't typed anything yet. The result was an error on
  // every single attempt, worded "Signed in, but that account doesn't
  // own X" at the one moment we knew for a fact nobody had signed in,
  // and it stayed up afterwards because nothing re-checked. Pressing
  // "Try again" appeared to fix it; really it just re-read the list once
  // the sign-in had happened in the meantime.
  //
  // The verdict now waits for evidence. The Rust side watches the window
  // and emits youtube-accounts-changed once auth cookies actually exist,
  // and again whenever the active channel changes - so the effect below
  // judges the result at a point where there is a result to judge, and
  // switching accounts in the still-open window settles it live.
  const authenticateChannel = async (ch: TrackedChannel) => {
    setYtBusy(true)
    setChannelsError(null)
    pendingAuthRef.current = ch
    pendingSlotRef.current = null
    try {
      // Remember WHICH slot this sign-in lands in. The ownership probe
      // has to read that slot's cookies specifically: asking for "any
      // account's" cookies returns the first slot that has some, which
      // after a few Authenticate clicks is somebody else entirely.
      pendingSlotRef.current = await invoke<string>("connect_youtube_account")
    } catch (e) {
      pendingAuthRef.current = null
      setChannelsError(String(e))
    } finally {
      setYtBusy(false)
    }
  }

  // Read the current launch-at-login state once on mount.
  useEffect(() => {
    isEnabled()
      .then(setAutostart)
      .catch(() => {})
  }, [])

  // Launch at login is on by default, and re-registered on every run.
  //
  // Two reasons this is not just "enable it if isEnabled() says off":
  // a backup tool the user has to remember to reopen is not a backup tool,
  // so the default has to be on; and isEnabled() only reports that a login
  // item EXISTS, not that it still points at a binary that exists. A login
  // item left by an older install keeps reporting "On" for years while the
  // OS silently fails to launch anything. So we rewrite it: disable() then
  // enable() puts the current build's path back into the registration.
  //
  // Gated on the user having signed in to ARCHIVE336, and nothing else.
  // It is tempting to also require setup to be READY, and that was the first
  // version of this - but it is exactly backwards. Setup readiness includes
  // "YouTube account connected", which lapses on its own when cookies expire.
  // Gating on it means the app stops registering to come back at the precise
  // moment it most needs to: the reboot after a session expired is the one
  // that would otherwise leave a user un-backed-up indefinitely, with the
  // app never opening again to tell them. Signed in = intends to use this,
  // so it should come back and say what it needs. Skipped once they say no.
  //
  // Packaged builds only. enable() records whatever binary is running, so
  // doing this under `tauri dev` registers target/debug/archive336 at login
  // - which is exactly how the plist this change exists to fix got written
  // in the first place, and a cargo clean turns it back into a login item
  // pointing at nothing. The Settings toggle still works in dev, and reads
  // its state from the OS, so nothing here claims a registration we skipped.
  useEffect(() => {
    if (!import.meta.env.PROD) return
    if (!config || config.autostartDeclined) return
    if (!config.username || !config.password) return
    if (autostartDeclinedRef.current) return
    if (autostartSyncedRef.current) return
    autostartSyncedRef.current = true
    void (async () => {
      try {
        // Clear first so the path gets rewritten rather than trusted. There
        // is nothing to clear on a fresh install (and Windows reports that
        // as an error), so a failure here must not stop the enable() below.
        await disable().catch(() => {})
        // Re-read the decision: the user can reach the toggle while the
        // await above is in flight, and their no outranks the default.
        if (!autostartDeclinedRef.current) {
          await enable()
          console.log("launch at login registered for this build")
        }
      } catch (e) {
        console.error("launch at login registration failed", e)
      }
      // Report what the OS actually holds, never what we asked for.
      setAutostart(await isEnabled().catch(() => false))
    })()
  }, [config])

  // Start as soon as setup is ready, once per app run.
  useEffect(() => {
    if (setupReady === "ready" && !status.running && !autoStartedRef.current) {
      autoStartedRef.current = true
      invoke("start_worker").catch((e) =>
        console.error("auto-start sync failed", e),
      )
    }
  }, [setupReady, status.running])

  // Helpers for the YouTube accounts list. Each account has its own
  // isolated session; list_youtube_accounts re-probes them all and
  // mirrors the aggregate up to the backend.
  const refreshAccounts = async () => {
    setYtBusy(true)
    try {
      const a = await invoke<YoutubeAccount[]>("list_youtube_accounts")
      setAccounts(a)
    } catch (e) {
      console.error("list accounts failed", e)
    } finally {
      setYtBusy(false)
    }
  }
  // Open a fresh sign-in window for a brand-new account, then show the
  // pending slot immediately. The user signs in, then clicks Re-check.
    // The setup checklist's Connect button: reuse the first disconnected
  // slot when one exists (no orphan-slot pileup), else add a fresh one.
  // Subscribe to status updates from the Rust worker
  useEffect(() => {
    const unlistenP = listen<WorkerStatus>("worker-status", (e) => {
      setStatus(e.payload)
    })
    return () => {
      void unlistenP.then((fn) => fn())
    }
  }, [])

  // The worker watches each open sign-in window and fires this event the
  // moment a sign-in completes - refresh so the card flips green on its
  // own (refreshing also closes the finished sign-in window).
  //
  // This is also where an Authenticate click is finally judged. The event
  // fires only once auth cookies exist, and again on every account switch
  // in the still-open window, which makes it the first honest moment to
  // ask whether the channel is owned - and a self-correcting one, since
  // switching to the right account fires it again.
  useEffect(() => {
    const unlisten = listen("youtube-accounts-changed", () => {
      void (async () => {
        // Order is load-bearing. list_youtube_accounts is what reports
        // our owned channels to the backend, so reading tracked-channels
        // before it returns asks the server a question it cannot answer
        // yet - which is half of why the old check never passed.
        await refreshAccounts()
        let fresh: TrackedChannel[]
        try {
          fresh = await invoke<TrackedChannel[]>("list_tracked_channels")
        } catch {
          return // leave whatever the list already showed
        }
        setTrackedChannels(fresh)
        setChannelsError(null)

        const want = pendingAuthRef.current
        if (!want) return
        const now = fresh.find((c) => c.youtubeId === want.youtubeId)
        if (now?.authenticated) {
          pendingAuthRef.current = null
          return
        }

        // Signing in succeeded. That is the whole of what the user was
        // asked to do, so it is never reported as a failure.
        //
        // Whether YouTube will hand this login the channel's private
        // videos is a separate question with a separate answer, and one
        // that can change later: a channel with no private uploads today
        // may have some tomorrow, and a brand channel may become
        // reachable when we can send a delegated identity. Gating the
        // sign-in on it meant a correct sign-in was answered with "Wrong
        // YouTube account", and a channel with no videos at all could
        // never be connected in advance of its first upload.
        //
        // The probe still runs, because proving access is what unlocks
        // private videos on the website. It just no longer decides
        // whether the sign-in counted.
        pendingAuthRef.current = null
        setProving(want.title)
        try {
          const res = await invoke<{ proven: boolean; publicOnly: boolean }>(
            "prove_channel_ownership",
            { youtubeId: want.youtubeId, accountId: pendingSlotRef.current },
          )
          if (res.proven) {
            // Proven access means the server can unlock this channel's
            // private videos, so re-read to pick up the real grant.
            await refreshAccounts()
          }
          // Either way the list is re-read: a completed sign-in shows as
          // authenticated on its own, whether or not the probe found
          // anything to prove.
          setTrackedChannels(
            await invoke<TrackedChannel[]>("list_tracked_channels"),
          )
        } catch {
          // A probe that could not run says nothing about the sign-in,
          // which already happened. Show it.
          setTrackedChannels(
            await invoke<TrackedChannel[]>("list_tracked_channels").catch(
              () => trackedChannels,
            ),
          )
        } finally {
          setProving(null)
        }
      })()
    })
    return () => {
      void unlisten.then((fn) => fn())
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // The website's Connect buttons deep-link into the app. Rust brings the
  // window forward; this hop lands the user on the tab the link is about.
  useEffect(() => {
    const unlisten = listen("deep-link-connect", () => {
      setTab("connections")
    })
    return () => {
      void unlisten.then((fn) => fn())
    }
  }, [])

  // Credentials are passed in explicitly rather than read from `config`.
  // The form inputs write into `config` on every keystroke, so anything
  // that reads sign-in creds from `config` reactively will act on a
  // half-typed password. Auto-sign-in (below) hands in the values it
  // loaded from disk; the form hands in what the user actually typed, only
  // when they submit.
  const signIn = async (creds: { username: string; password: string }) => {
    if (!creds.username || !creds.password) return
    setSigninBusy(true)
    setSigninError(null)
    try {
      await invoke("signin_now", {
        username: creds.username,
        password: creds.password,
      })
      // Now signed in: refresh accounts so the worker reports its
      // connection up to the backend (the website mirrors it for Basic).
      void refreshAccounts()
    } catch (err) {
      setSigninError(String(err))
    } finally {
      setSigninBusy(false)
    }
  }

  const onSignIn = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    if (!config) return
    void signIn({ username: config.username, password: config.password })
  }

  const onSignOut = async () => {
    setSigninBusy(true)
    setSigninError(null)
    try {
      await invoke("signout_now")
      // Sign-out clears the stored credentials on the Rust side, so re-read
      // them - otherwise the checklist keeps rendering the old config and
      // still shows "Account credentials saved" until the next launch.
      const fresh = await invoke<StoredConfig>("get_credentials")
      setConfig(fresh)
      // Re-arm the worker's one-shot auto-start guard so a sign-out/sign-in
      // within one app run actually restarts syncing rather than sitting
      // idle behind a spent ref. (Auto-sign-in has no such reset: it fires
      // only from the on-disk load at launch, never from a manual sign-in,
      // so signing back in goes through the Sign in button directly.)
      autoStartedRef.current = false
    } catch (err) {
      setSigninError(String(err))
    } finally {
      setSigninBusy(false)
    }
  }

  // Not a Start button. Syncing is automatic; this only exists for the one
  // state the app cannot get itself out of - the worker stopped because
  // something was wrong (revoked access, a signed-out account), fixed it, and
  // needs one nudge rather than a quit and relaunch.
  const onRetry = async () => {
    setBusy(true)
    try {
      await invoke("start_worker")
    } catch (err) {
      alert(String(err))
    } finally {
      setBusy(false)
    }
  }

  // Remember an explicit "no" (and clear it on an explicit "yes") so the
  // registration pass above never re-enables what the user just switched
  // off. Mirrored into local config state as well, so a toggle made during
  // this run is respected without waiting for a relaunch.
  const rememberAutostartChoice = async (declined: boolean) => {
    // Synchronously, before any await: the registration pass may be sitting
    // between its disable() and its enable() right now, and this is the only
    // value it can read that reflects a click made this instant.
    autostartDeclinedRef.current = declined
    try {
      await invoke("set_autostart_declined", { declined })
    } catch (e) {
      console.error("saving launch-at-login choice failed", e)
    }
    setConfig((c) => (c ? { ...c, autostartDeclined: declined } : c))
  }

  const toggleAutostart = async () => {
    const turningOff = autostart
    setAutostartBusy(true)
    try {
      // Record the choice before touching the OS: an explicit no has to
      // survive a failed disable(), or the next launch re-enables it.
      await rememberAutostartChoice(turningOff)
      try {
        if (turningOff) await disable()
        else await enable()
      } catch (err) {
        console.error("autostart toggle failed", err)
      }
      // The button reports what the OS holds, not what we asked for.
      setAutostart(await isEnabled().catch(() => false))
    } finally {
      setAutostartBusy(false)
    }
  }

  if (!config) {
    return (
      <main className="p-4 max-w-xl mx-auto">
        <p className="text-sm text-muted-foreground">Loading…</p>
      </main>
    )
  }

  // Resolve the URL the "Open archive" link opens. Prefer the user's
  // configured server (so a self-hosted user is sent to *their* host),
  // fall back to the production domain.
  const archiveUrl =
    config.base_url?.replace(/\/+$/, "") || "https://archive336.com"

  // The header's "Open archive" goes to the root; "Add a channel" has to
  // land on the page that actually has the Track bar, or the button names
  // an action the destination doesn't offer.
  const openAddChannel = async () => {
    try {
      await openUrl(`${archiveUrl}/youtube`)
    } catch (err) {
      console.error("open add-channel failed", err)
    }
  }

  const openArchive = async () => {
    try {
      await openUrl(archiveUrl)
    } catch (err) {
      console.error("openUrl failed", err)
    }
  }

  // Stopped is no longer a resting state, so it is never shown as a calm
  // "Stopped" the user is expected to fix. Either we are running, or setup is
  // unfinished, or something went wrong and says so.
  // Running now covers waiting, too: the worker holds itself open through a
  // backoff when there is no network yet, no connected YouTube account, or a
  // session it is logging back in for. That is worth staying open for, but it
  // is not "Watching" - a green pill over a worker that is asleep on an error
  // is the same lie as a Stopped worker claiming to sync. While an error is
  // standing, the headline says Retrying and goes amber; a successful poll
  // clears the error on the Rust side and it goes back to green by itself.
  const syncStatusLabel = status.running
    ? status.lastError
      ? "Retrying"
      : status.currentJobId
        ? "Working"
        : "Watching"
    : setupReady !== "ready"
      ? "Setup needed"
      : status.lastError
        ? "Stopped"
        : "Starting"
  const syncNote = status.running
    ? status.loggedIn
      ? null
      : "Signing in…"
    : setupReady !== "ready"
      ? "Finish setup above and syncing starts on its own"
      : status.lastError
        ? "This will not restart on its own - press Retry once the problem below is fixed."
        : "Syncing starts automatically"

  const syncStatus: "active" | "warning" | "idle" | "down" = status.running
    ? status.lastError
      ? "warning"
      : "active"
    : status.lastError
      ? "down"
      : "idle"

  return (
    <main className="p-4 max-w-xl mx-auto">
      {/* Top bar: tab nav on the left, link to the website on the right. */}
      <header className="flex items-center justify-between gap-4 border-b border-border">
        <nav className="flex">
          <TabButton
            label="Status"
            active={tab === "status"}
            onClick={() => setTab("status")}
          />
          <TabButton
            label="Connections"
            active={tab === "connections"}
            onClick={() => setTab("connections")}
          />
          <TabButton
            label="Settings"
            active={tab === "settings"}
            onClick={() => setTab("settings")}
          />
        </nav>
        <Button
          variant="outline"
          size="sm"
          onClick={openArchive}
          title={archiveUrl}
          className="gap-1.5"
        >
          Open archive
          <ExternalLink className="h-3 w-3" />
        </Button>
      </header>

      <div className="mt-4 space-y-4">
        {tab === "status" && (
          <>
            <SetupChecklist
              hasCredentials={!!config.username && !!config.password}
              ytdlpInstalled={!ytdlpError}
              onReadinessChange={setSetupReady}
              trackedChannelCount={
                channelsLoading || channelsError !== null
                  ? null
                  : trackedChannels.length
              }
              onAddChannel={openAddChannel}
            />

            <AccountBox
              name="Sync"
              status={syncStatus}
              statusLabel={syncStatusLabel}
              actions={
                // Only when the worker stopped on an error. There is
                // deliberately no control in the normal case: quitting the
                // app is how you stop syncing.
                !status.running && status.lastError ? (
                  <Button size="sm" onClick={onRetry} disabled={busy}>
                    Retry
                  </Button>
                ) : undefined
              }
            >
              {/* Nothing to say in the steady state: the pill beside the
                  heading already reads Watching / Working, and who you are
                  signed in as belongs on Settings, where it is. Every line
                  below describes something the user might act on.

                  A stopped-on-error worker does NOT start itself again -
                  the auto-start is once per app run, deliberately, so a
                  revocation the user asked for stays honoured. Saying
                  "syncing starts automatically" there would be a promise
                  the app has already decided not to keep. */}
              {syncNote && (
                <div className="text-xs text-muted-foreground">{syncNote}</div>
              )}

              {status.currentJobId && (
                <div className="mt-3 pt-3 border-t border-border">
                  <div className="text-sm font-semibold mb-2">
                    {status.currentVideoTitle ||
                      status.currentVideoId ||
                      "…"}
                  </div>
                  <div className="h-1 bg-muted overflow-hidden">
                    <div
                      className="h-full bg-primary"
                      style={{
                        width: `${Math.round(status.currentProgress * 100)}%`,
                      }}
                    />
                  </div>
                  <div className="text-xs text-muted-foreground mt-1">
                    {Math.round(status.currentProgress * 100)}% · job{" "}
                    {status.currentJobId.slice(0, 8)}
                  </div>
                </div>
              )}

              {status.completedCount > 0 && (
                <div className="mt-3 pt-3 border-t border-border text-xs text-muted-foreground">
                  {status.completedCount} synced this session
                  {status.lastCompletedVideoId && (
                    <>
                      {" · last: "}
                      <code className="font-mono bg-muted px-1 py-0.5">
                        {status.lastCompletedVideoId}
                      </code>
                    </>
                  )}
                </div>
              )}

              {status.lastError && (
                <div className="mt-3 p-3 border border-destructive text-xs text-destructive">
                  {status.lastError}
                </div>
              )}
            </AccountBox>
          </>
        )}

        {tab === "connections" && (
          <div className="space-y-4">
            {/* Channels, not Google accounts.
                This tab used to list "YouTube account 1/2/3" and let you
                connect any account you liked, which allowed connecting a
                channel the website had never heard of - a silent no-op that
                still turned the setup checklist green. The website is the
                only source of this list now, so a channel missing here is a
                channel not yet added there. */}
            {channelsLoading ? (
              <p className="text-sm text-muted-foreground">Checking channels…</p>
            ) : channelsError && trackedChannels.length === 0 ? (
              /* Only takes the whole panel when there is nothing to take
                 it from. A failed re-check while channels are on screen
                 used to replace all of them with one red box, which
                 throws away true, useful information - an authenticated
                 channel is still authenticated - to report a request
                 that failed. The banner below covers that case instead. */
              <div className="border border-destructive p-4">
                <p className="text-sm font-semibold mb-1">
                  Couldn't reach your archive
                </p>
                <p className="text-xs text-muted-foreground mb-3">
                  {channelsError}
                </p>
                <Button size="sm" onClick={() => refreshChannels()}>
                  Try again
                </Button>
              </div>
            ) : trackedChannels.length === 0 ? (
              /* Not an error - there is genuinely nothing to authenticate
                 until a channel is added on the website, which is also
                 where billing starts. Point there instead of offering a
                 Connect button that cannot accomplish anything.

                 Named by SOURCE, never "channels". YouTube is the first
                 source this app backs up, not the only one it ever will,
                 and a generic empty state would have to be rewritten the
                 day Twitch lands. Each source gets its own labelled block
                 and its own "Add <source>" action instead. */
              <section>
                <h2 className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">
                  YouTube
                </h2>
                {/* Same shape as the website's connect card: neutral icon
                    in a bordered square, label, action on the right. The
                    destructive border and button carry the urgency - with
                    no channel there is nothing for this app to back up,
                    which is the one state that stops it being useful. */}
                <div className="border border-destructive p-4 flex items-center gap-4">
                  <div className="size-10 shrink-0 border border-border flex items-center justify-center">
                    <MonitorPlay className="size-5 text-muted-foreground" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-semibold">
                      No YouTube channels yet
                    </div>
                  </div>
                  <Button variant="destructive" onClick={openAddChannel}>
                    <Plus />
                    Add YouTube channel
                  </Button>
                </div>
              </section>
            ) : (
              <section>
                {/* Label left, refresh right - same header idiom as the
                    website's Section. A full-width Refresh bar under the
                    list read like a primary action, which it is not: it
                    is a manual re-poll of a list that already refreshes
                    itself on launch and after sign-in. */}
                <div className="flex items-center justify-between gap-4 mb-3">
                  <h2 className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold">
                    YouTube
                  </h2>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    onClick={() => refreshChannels()}
                    disabled={channelsLoading}
                    title="Refresh channels"
                    aria-label="Refresh channels"
                  >
                    <RefreshCw />
                  </Button>
                </div>
                {/* Sits above the list rather than replacing it. The old
                    code routed this through channelsError, which is the
                    "server unreachable" slot, so a wrong-account sign-in
                    hid every channel behind a card headed "Couldn't reach
                    your archive" - a claim about the network, made about
                    a request that had just succeeded. No retry button:
                    switching account in the open window re-fires the
                    watcher and clears this on its own. */}
                {proving && (
                  <div className="border border-border p-3 mb-4">
                    <p className="text-xs text-muted-foreground">
                      Checking whether this account can reach {proving}…
                    </p>
                  </div>
                )}
                {/* A re-check that failed while we already have a list.
                    Worth mentioning, not worth hiding the channels over -
                    the list on screen is the last known good answer and
                    is almost certainly still correct. */}
                {channelsError && (
                  <div className="border border-border p-3 mb-4 flex items-center gap-3">
                    <p className="text-xs text-muted-foreground min-w-0 flex-1">
                      Couldn't refresh this list. Showing what we last saw.
                    </p>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => refreshChannels()}
                    >
                      Retry
                    </Button>
                  </div>
                )}
                <div className="space-y-4">
                {trackedChannels.map((ch) => (
                  <AccountBox
                    key={ch.youtubeId}
                    name={ch.title}
                    // No pill at all until the channel is authenticated.
                    // Not connecting one is a deliberate choice - plenty
                    // of people connect some channels and not others - so
                    // there is nothing to report about it. The
                    // Authenticate button already offers the action; a
                    // badge beside it would only be labelling the absence
                    // of one. Once authenticated it earns a green pill,
                    // because THAT is a state worth confirming.
                    status={ch.authenticated ? "active" : undefined}
                    statusLabel={ch.authenticated ? "Authenticated" : undefined}
                    actions={
                      <>
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={ytBusy}
                          onClick={() => authenticateChannel(ch)}
                        >
                          {ch.authenticated ? "Re-authenticate" : "Authenticate"}
                        </Button>
                      </>
                    }
                  />
                ))}
                </div>
              </section>
            )}
          </div>
        )}

        {tab === "settings" && (
          <>
            {/* Label sits OUTSIDE the card, matching the Connections tab
                and the website's Section idiom: the tiny uppercase label
                names the group, the bordered box holds only content. */}
            <section>
              <h2 className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">
                Account
              </h2>
              <div className="border border-border p-4">
              {status.loggedIn ? (
                <>
                  <div className="flex items-center justify-between gap-4">
                    <p className="text-xs text-muted-foreground min-w-0">
                      Signed in as{" "}
                      <strong className="text-foreground">
                        {config.username}
                      </strong>
                      .
                    </p>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={onSignOut}
                      disabled={signinBusy}
                      className="shrink-0"
                    >
                      {signinBusy ? "Signing out…" : "Sign out"}
                    </Button>
                  </div>
                  {signinError && (
                    <p className="text-xs text-destructive mt-2">
                      {signinError}
                    </p>
                  )}
                </>
              ) : (
                <form onSubmit={onSignIn} className="space-y-3">
                  <FormField label="Username">
                    <input
                      type="text"
                      autoComplete="username"
                      value={config.username}
                      onChange={(e) =>
                        setConfig({ ...config, username: e.currentTarget.value })
                      }
                      className="h-8 w-full border border-border bg-input px-3 text-xs focus:outline-none focus:border-ring"
                    />
                  </FormField>
                  <FormField label="Password">
                    <input
                      type="password"
                      autoComplete="current-password"
                      value={config.password}
                      onChange={(e) =>
                        setConfig({ ...config, password: e.currentTarget.value })
                      }
                      className="h-8 w-full border border-border bg-input px-3 text-xs focus:outline-none focus:border-ring"
                    />
                  </FormField>
                  <Button
                    type="submit"
                    size="sm"
                    disabled={signinBusy || !config.username || !config.password}
                  >
                    {signinBusy ? "Signing in…" : "Sign in"}
                  </Button>
                  {signinError && (
                    <p className="text-xs text-destructive">{signinError}</p>
                  )}
                </form>
              )}
              </div>
            </section>

            <div className="flex items-center justify-between gap-4 border border-border p-4">
              <div className="min-w-0 flex-1">
                <div className="text-sm font-semibold">Launch at login</div>
              </div>
              <Switch
                checked={autostart}
                onCheckedChange={() => toggleAutostart()}
                disabled={autostartBusy}
                aria-label="Launch at login"
              />
            </div>

          </>
        )}
      </div>
    </main>
  )
}

function TabButton({
  label,
  active,
  onClick,
}: {
  label: string
  active: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "px-4 py-2.5 text-sm font-semibold cursor-pointer border-b-2 -mb-px whitespace-nowrap",
        active
          ? "border-foreground text-foreground"
          : "border-transparent text-muted-foreground hover:text-foreground",
      )}
    >
      {label}
    </button>
  )
}

function FormField({
  label,
  children,
}: {
  label: string
  children: React.ReactNode
}) {
  return (
    <label className="block">
      <span className="block text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-1">
        {label}
      </span>
      {children}
    </label>
  )
}

export default App
