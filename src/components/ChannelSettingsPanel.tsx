import * as React from "react"
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetBody,
  SheetFooter,
} from "./ui/sheet"
import { Button } from "./ui/button"
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "./ui/dialog"
import { Switch } from "./ui/switch"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "./ui/select"
import { Lock, Plus, ShieldCheck, ShieldOff, Trash2 } from "lucide-react"
import { useToast } from "./ui/toast"
import type {
  Channel,
  ChannelArchiveSettings,
  CodecPreference,
  FilterPreset,
  MetadataRefreshFrequency,
  VideoCardMetaField,
  VideoMaxResolution,
} from "@/lib/types"
import { cn } from "@/lib/utils"
import { formatFullDate, formatGb } from "@/lib/format"
import {
  estimateMonthlyStorageCostUsd,
  projectedGbPerHour,
} from "@/lib/estimates"
import { usePrices } from "@/lib/pricing"
import { PresetEditor, generatePresetId } from "./PresetEditor"

type Props = {
  channel: Channel
  settings: ChannelArchiveSettings
  open: boolean
  onOpenChange: (open: boolean) => void
  onSave: (settings: ChannelArchiveSettings) => void | Promise<void>
  /** The account-wide settings. The "Global settings" section of this
   *  panel edits THESE, not the channel's copy - the heading has always
   *  said global, and until now the controls under it wrote per-channel,
   *  so four channels could each show different meta fields while every
   *  one of them claimed the setting was global. */
  globalSettings: ChannelArchiveSettings
  onSaveGlobal: (settings: ChannelArchiveSettings) => void | Promise<void>
  /** Re-apply the user's account defaults to this channel. Resolves with
   *  the settings the server actually stored. */
  onReset: () => Promise<ChannelArchiveSettings>
  onRemove?: () => void | Promise<void>
  /** Best-available estimate of the channel's full-catalog duration in seconds. */
  estimatedChannelDurationSec: number
  /** Actual archived footage, for anchoring the full-channel size estimate
      to the user's real bytes-per-hour instead of a generic bitrate guess. */
  archivedBytes: number
  archivedDurationSec: number
}


const RESOLUTION_OPTIONS: { value: VideoMaxResolution; label: string }[] = [
  { value: "source", label: "Source (best available)" },
  { value: "2160p", label: "2160p (4K)" },
  { value: "1440p", label: "1440p" },
  { value: "1080p", label: "1080p" },
  { value: "720p", label: "720p" },
  { value: "480p", label: "480p" },
  { value: "360p", label: "360p" },
  { value: "audio-only", label: "Audio only" },
]

const CODEC_OPTIONS: { value: CodecPreference; label: string }[] = [
  { value: "compat", label: "Compatibility (H.264 / AAC)" },
  { value: "efficient", label: "Efficient (AV1 / VP9 when available)" },
]

const METADATA_REFRESH_OPTIONS: {
  value: MetadataRefreshFrequency
  label: string
}[] = [
  { value: "weekly", label: "Weekly" },
  { value: "monthly", label: "Monthly" },
  { value: "quarterly", label: "Quarterly" },
  { value: "annually", label: "Annually" },
]

const META_FIELD_OPTIONS: { value: VideoCardMetaField; label: string }[] = [
  { value: "uploadDate", label: "Upload date" },
  { value: "duration", label: "Duration" },
  { value: "fileSize", label: "File size" },
  { value: "type", label: "Type" },
]

export function ChannelSettingsPanel({
  channel,
  settings,
  open,
  onOpenChange,
  onSave,
  globalSettings,
  onSaveGlobal,
  onReset,
  onRemove,
  estimatedChannelDurationSec,
  archivedBytes,
  archivedDurationSec,
}: Props) {
  const [draft, setDraft] = React.useState<ChannelArchiveSettings>(settings)
  const [globalDraft, setGlobalDraft] =
    React.useState<ChannelArchiveSettings>(globalSettings)
  const [resetOpen, setResetOpen] = React.useState(false)
  const [resetting, setResetting] = React.useState(false)

  const handleReset = async () => {
    if (resetting) return
    setResetting(true)
    try {
      const applied = await onReset()
      // Take what the SERVER stored, not what we assumed it would store.
      // The whole reason this button exists is that a local idea of the
      // defaults drifted from the real ones.
      setDraft(applied)
      setResetOpen(false)
    } catch {
      // onReset toasts; leave the dialog open so it can be retried.
    } finally {
      setResetting(false)
    }
  }


  // Reset draft each time the panel re-opens with the latest committed settings.
  React.useEffect(() => {
    if (open) setDraft(settings)
  }, [open, settings])

  React.useEffect(() => {
    if (open) setGlobalDraft(globalSettings)
  }, [open, globalSettings])

  const dirty =
    JSON.stringify(draft) !== JSON.stringify(settings) ||
    JSON.stringify(globalDraft) !== JSON.stringify(globalSettings)

  const [saving, setSaving] = React.useState(false)
  const handleSave = async () => {
    if (saving) return
    setSaving(true)
    try {
      // Both, and the channel first: if the global write fails the panel
      // stays open with the per-channel change already safe, rather than
      // losing both to one failure.
      await onSave(draft)
      if (
        JSON.stringify(globalDraft) !== JSON.stringify(globalSettings)
      ) {
        await onSaveGlobal(globalDraft)
      }
      onOpenChange(false)
    } catch {
      // onSave already toasted the error - keep the panel open so the user
      // can see their unsaved changes and retry.
    } finally {
      setSaving(false)
    }
  }

  const [removing, setRemoving] = React.useState(false)
  const [removeConfirmOpen, setRemoveConfirmOpen] = React.useState(false)
  const handleRemove = async () => {
    if (!onRemove || removing) return
    setRemoving(true)
    try {
      await onRemove()
      // Deliberately do NOT onOpenChange(false) here. This panel's open
      // state lives in the URL (?panel=settings), so closing it writes the
      // search params of the CURRENT path. onRemove navigates the user away
      // (removal is terminal), and that navigation and this param-write are
      // both history writes in the same tick - the param-write runs last and
      // clobbered the navigation, landing the user right back on the page
      // they just removed. onRemove owns what happens on success; closing
      // only the local confirm dialog here is enough, and the navigation
      // unmounts the panel anyway.
      setRemoveConfirmOpen(false)
    } catch {
      // onRemove already toasted - keep the panel open so the user can retry.
    } finally {
      setRemoving(false)
    }
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent>
        <SheetHeader>
          <SheetTitle>{channel.name} settings</SheetTitle>
        </SheetHeader>

        <SheetBody className="space-y-6">
          {/* Active on the left, reset on the right. Both act on the whole
              channel rather than on any one section, so they sit above the
              sections instead of inside one. */}
          <div className="flex items-center justify-between gap-4">
            <div className="flex items-center gap-2">
              <span className="text-sm font-semibold">Active</span>
              <Switch
                checked={draft.active}
                onCheckedChange={(v) => setDraft({ ...draft, active: v })}
                aria-label="Channel active"
              />
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setResetOpen(true)}
              disabled={resetting}
            >
              {resetting ? "Resetting…" : "Reset to default settings"}
            </Button>
          </div>

          <AuthenticationSection
            channelId={channel.id}
            channelName={channel.name || channel.handle || "this channel"}
            ownershipRevoked={channel.ownershipRevoked}
          />

          <div
            className="space-y-6"
          >
          <Section label="Channel info" headerRight={<MetadataColumnLabels />}>
            <MetadataToggleRow
              label="Profile Picture"
              captured={draft.saveChannelAvatar}
              onCapturedChange={(v) =>
                setDraft({ ...draft, saveChannelAvatar: v })
              }
              history={draft.saveChannelAvatarHistory}
              onHistoryChange={(v) =>
                setDraft({ ...draft, saveChannelAvatarHistory: v })
              }
            />
            <MetadataToggleRow
              label="About"
              captured={draft.saveChannelAbout}
              onCapturedChange={(v) =>
                setDraft({ ...draft, saveChannelAbout: v })
              }
              history={draft.saveChannelAboutHistory}
              onHistoryChange={(v) =>
                setDraft({ ...draft, saveChannelAboutHistory: v })
              }
            />
            <MetadataToggleRow
              label="Channel stats"
              captured={draft.saveChannelStatsSnapshots}
              onCapturedChange={(v) =>
                setDraft({ ...draft, saveChannelStatsSnapshots: v })
              }
              history={draft.saveChannelStatsHistory}
              onHistoryChange={(v) =>
                setDraft({ ...draft, saveChannelStatsHistory: v })
              }
            />
          </Section>

          <Divider />

          <Section label="Video sync">
            <ToggleRow
              label="Automatically sync"
              checked={draft.downloadNewVideos}
              onChange={(v) => setDraft({ ...draft, downloadNewVideos: v })}
            />
            <ToggleRow
              label="Include metadata"
              checked={draft.includeMetadataOnVideoSync}
              onChange={(v) =>
                setDraft({ ...draft, includeMetadataOnVideoSync: v })
              }
            />
            <Field label="Max resolution">
              <Select
                value={draft.maxResolution}
                onValueChange={(v) =>
                  setDraft({ ...draft, maxResolution: v as VideoMaxResolution })
                }
              >
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {RESOLUTION_OPTIONS.map((o) => (
                    <SelectItem key={o.value} value={o.value}>
                      {o.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
            <Field label="Codec preference">
              <Select
                value={draft.codecPreference}
                onValueChange={(v) =>
                  setDraft({ ...draft, codecPreference: v as CodecPreference })
                }
              >
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {CODEC_OPTIONS.map((o) => (
                    <SelectItem key={o.value} value={o.value}>
                      {o.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>

            <EstimateBlock
              resolution={draft.maxResolution}
              codec={draft.codecPreference}
              channelDurationSec={estimatedChannelDurationSec}
              measured={
                archivedBytes > 0 && archivedDurationSec > 0
                  ? {
                      bytes: archivedBytes,
                      durationSec: archivedDurationSec,
                      // Archived footage was captured at whatever the
                      // channel's settings were then; the saved settings are
                      // our best proxy for that quality.
                      resolution: settings.maxResolution,
                      codec: settings.codecPreference,
                    }
                  : undefined
              }
            />
          </Section>

          <Divider />

          <Section
            label="Metadata sync"
            headerRight={<MetadataColumnLabels />}
          >
            <MetadataToggleRow
              label="Thumbnail"
              captured={draft.saveThumbnail}
              onCapturedChange={(v) => setDraft({ ...draft, saveThumbnail: v })}
              history={draft.saveThumbnailHistory}
              onHistoryChange={(v) =>
                setDraft({ ...draft, saveThumbnailHistory: v })
              }
            />
            <MetadataToggleRow
              label="View count"
              captured={draft.saveViewCount}
              onCapturedChange={(v) => setDraft({ ...draft, saveViewCount: v })}
              history={draft.saveViewCountHistory}
              onHistoryChange={(v) =>
                setDraft({ ...draft, saveViewCountHistory: v })
              }
            />
            <MetadataToggleRow
              label="Description"
              captured={draft.saveDescription}
              onCapturedChange={(v) =>
                setDraft({ ...draft, saveDescription: v })
              }
              history={draft.saveDescriptionHistory}
              onHistoryChange={(v) =>
                setDraft({ ...draft, saveDescriptionHistory: v })
              }
            />
            <MetadataToggleRow
              label="Tags"
              captured={draft.saveTags}
              onCapturedChange={(v) => setDraft({ ...draft, saveTags: v })}
              history={draft.saveTagsHistory}
              onHistoryChange={(v) =>
                setDraft({ ...draft, saveTagsHistory: v })
              }
            />
            <MetadataToggleRow
              label="Captions"
              captured={draft.saveCaptions}
              onCapturedChange={(v) => setDraft({ ...draft, saveCaptions: v })}
              history={draft.saveCaptionsHistory}
              onHistoryChange={(v) =>
                setDraft({ ...draft, saveCaptionsHistory: v })
              }
            />
            <Field label="Update frequency">
              <Select
                value={draft.metadataRefreshFrequency}
                onValueChange={(v) =>
                  setDraft({
                    ...draft,
                    metadataRefreshFrequency: v as MetadataRefreshFrequency,
                  })
                }
              >
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {METADATA_REFRESH_OPTIONS.map((o) => (
                    <SelectItem key={o.value} value={o.value}>
                      {o.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
          </Section>

          <Divider />

          <Section label="Comments sync">
            {/* This used to be disabled unless the channel was
                authenticated, with copy telling the user to go and do that
                first - a false instruction. The comment needing OAuth was
                true of the YouTube Data API era; the worker runs yt-dlp
                with --write-comments, which needs no credentials for a
                public video. Authentication only extends coverage to
                private and unlisted comments, which the Authentication
                card above already says - repeating it under this toggle
                just made the row taller. */}
            <ToggleRow
              label="Sync comments"
              checked={draft.syncComments}
              onChange={(v) => setDraft({ ...draft, syncComments: v })}
            />
            <Field label="Update frequency">
              <Select
                value={draft.commentsRefreshFrequency}
                onValueChange={(v) =>
                  setDraft({
                    ...draft,
                    commentsRefreshFrequency: v as MetadataRefreshFrequency,
                  })
                }
              >
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {METADATA_REFRESH_OPTIONS.map((o) => (
                    <SelectItem key={o.value} value={o.value}>
                      {o.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
          </Section>

          </div>

          <ScopeBreak label="Global settings" />

          <Section label="Video previews">
            <ToggleRow
              label="Status badge"
              checked={globalDraft.showStatusBadges}
              onChange={(v) => setGlobalDraft({ ...globalDraft, showStatusBadges: v })}
            />
            <ToggleRow
              label="Status border"
              checked={globalDraft.useStatusColorBorder}
              onChange={(v) => setGlobalDraft({ ...globalDraft, useStatusColorBorder: v })}
            />
            <div>
              <div className="text-sm font-semibold mb-2">Meta fields</div>
              <div className="flex flex-wrap gap-1.5">
                {META_FIELD_OPTIONS.map((o) => {
                  const active = globalDraft.cardMetaFields.includes(o.value)
                  return (
                    <button
                      key={o.value}
                      type="button"
                      onClick={() => {
                        setGlobalDraft({
                          ...draft,
                          cardMetaFields: active
                            ? globalDraft.cardMetaFields.filter((f) => f !== o.value)
                            : [...globalDraft.cardMetaFields, o.value],
                        })
                      }}
                      className={cn(
                        "px-3 py-1 text-xs font-semibold border cursor-pointer",
                        active
                          ? "bg-white text-black border-white"
                          : "border-border text-foreground"
                      )}
                    >
                      {o.label}
                    </button>
                  )
                })}
              </div>
            </div>
          </Section>

          <Divider />

          <Section label="Filter presets">
            <div className="space-y-2">
              {globalDraft.filterPresets.map((preset, idx) => (
                <PresetEditor
                  key={preset.id}
                  preset={preset}
                  onUpdate={(updated) => {
                    const next = [...globalDraft.filterPresets]
                    next[idx] = updated
                    setGlobalDraft({ ...globalDraft, filterPresets: next })
                  }}
                  onDelete={() => {
                    setGlobalDraft({
                      ...draft,
                      filterPresets: globalDraft.filterPresets.filter(
                        (p) => p.id !== preset.id
                      ),
                    })
                  }}
                />
              ))}
            </div>
            <button
              type="button"
              onClick={() => {
                const newPreset: FilterPreset = {
                  id: generatePresetId(),
                  label: "New preset",
                  locked: false,
                  search: "",
                  visibilities: [],
                  types: [],
                  dateFrom: "",
                  dateTo: "",
                  sortDimension: "upload",
                  sortDirection: "desc",
                  viewMode: "grid",
                }
                setGlobalDraft({
                  ...draft,
                  filterPresets: [...globalDraft.filterPresets, newPreset],
                })
              }}
              className="w-full border border-dashed border-border px-3 py-2 text-sm font-semibold text-muted-foreground cursor-pointer flex items-center justify-center gap-2 mt-3"
            >
              <Plus className="size-4" /> Add preset
            </button>
          </Section>

        </SheetBody>

        <Dialog open={resetOpen} onOpenChange={setResetOpen}>
          <DialogContent className="max-w-md">
            <div className="p-6 space-y-5">
              <DialogHeader>
                <DialogTitle>Reset to default settings?</DialogTitle>
              </DialogHeader>
              <p className="text-sm text-muted-foreground leading-relaxed">
                Replaces this channel's settings with your New channel
                defaults. Nothing already archived is affected, and the
                channel stays {draft.active ? "active" : "paused"}.
              </p>
              <DialogFooter>
                <Button
                  variant="ghost"
                  onClick={() => setResetOpen(false)}
                  disabled={resetting}
                >
                  Cancel
                </Button>
                <Button onClick={handleReset} disabled={resetting}>
                  {resetting ? "Resetting…" : "Reset"}
                </Button>
              </DialogFooter>
            </div>
          </DialogContent>
        </Dialog>

        <SheetFooter className="justify-between">
          {onRemove ? (
            <button
              type="button"
              onClick={() => setRemoveConfirmOpen(true)}
              disabled={removing}
              className="border-2 border-destructive text-white font-bold px-4 py-1.5 text-sm cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Remove channel
            </button>
          ) : (
            <span />
          )}
          <div className="flex items-center gap-2">
            <Button variant="ghost" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button onClick={handleSave} disabled={!dirty || saving}>
              {saving ? "Saving…" : "Save changes"}
            </Button>
          </div>
        </SheetFooter>
      </SheetContent>

      <Dialog
        open={removeConfirmOpen}
        onOpenChange={(open) => {
          if (!removing) setRemoveConfirmOpen(open)
        }}
      >
        <DialogContent className="max-w-md">
          <div className="p-6 space-y-5">
            <DialogHeader>
              <DialogTitle>Remove this channel?</DialogTitle>
            </DialogHeader>
            <p className="text-sm text-muted-foreground leading-relaxed">
              Moves{" "}
              <span className="font-semibold text-foreground">
                {channel.name || channel.handle || "this channel"}
              </span>{" "}
              and its videos to{" "}
              <span className="font-semibold text-foreground">
                Recently removed
              </span>
              . You can restore for <strong>30 days</strong>; after
              that the data is permanently deleted. This is what stops
              the storage charges: they stop the moment you remove it, and
              stay stopped for as long as it sits in Recently removed.
            </p>
            <DialogFooter>
              <Button
                variant="ghost"
                onClick={() => setRemoveConfirmOpen(false)}
                disabled={removing}
              >
                Cancel
              </Button>
              <Button
                variant="destructive"
                onClick={() => void handleRemove()}
                disabled={removing}
              >
                <Trash2 />
                {removing ? "Removing…" : "Remove channel"}
              </Button>
            </DialogFooter>
          </div>
        </DialogContent>
      </Dialog>
    </Sheet>
  )
}

type AuthStatus = {
  authenticated: boolean
  workerConnected: boolean
  /** True when the user turned this authentication back off themselves. */
  userRevoked?: boolean
  /** When they turned it off. Null while authentication is live. */
  revokedAt?: string | null
}

// Per-channel authentication: is this user a verified owner (so the
// channel's sealed videos sync) and is a worker connected. Status is
// fetched fresh each time the panel opens. Establishing ownership happens
// in the worker app (a website can't capture a Google login); this
// surfaces the state plus the path to do it. Disconnecting is the half we
// can drive from here - the server stops honouring the ownership claim and
// lists the channel as revoked, which is the worker's instruction to sign
// that account out of its webview - so it lives on this card. The copy
// scopes itself to the one YouTube account, never the worker app as a
// whole: the worker connection is per-user and survives this.
function AuthenticationSection({
  channelId,
  channelName,
  ownershipRevoked,
}: {
  channelId: string
  channelName: string
  ownershipRevoked?: boolean
}) {
  const { toast } = useToast()
  const [status, setStatus] = React.useState<AuthStatus | null>(null)
  // Seeded from the channel payload so the card is right before (and if)
  // the status fetch lands. Saying "unlocked" while a revocation is in
  // force would be a promise we aren't keeping.
  const [revoked, setRevoked] = React.useState(ownershipRevoked === true)
  const [revokedAt, setRevokedAt] = React.useState<string | null>(null)
  const [pending, setPending] = React.useState(false)
  const [revokeConfirmOpen, setRevokeConfirmOpen] = React.useState(false)

  // The channel the user has acted on from this card, if any. Anything that
  // was already in flight when they hit the button describes the world
  // before the click, and the channel payload the panel was opened with is
  // older still. Letting either land afterwards would flip the card back and
  // read exactly like the revoke undoing itself, so both defer to the action.
  const actedForRef = React.useRef<string | null>(null)

  React.useEffect(() => {
    if (actedForRef.current === channelId) return
    setRevoked(ownershipRevoked === true)
  }, [channelId, ownershipRevoked])

  React.useEffect(() => {
    let cancelled = false
    const read = () => {
      fetch(
        `/api/youtube/channels/${encodeURIComponent(channelId)}/authentication`,
        { credentials: "include" }
      )
        .then((r) => (r.ok ? r.json() : null))
        .then((d: AuthStatus | null) => {
          if (cancelled || !d || actedForRef.current === channelId) return
          setStatus(d)
          // Only let the response speak for the revoked flag when it
          // actually carries one, so an older payload can't quietly
          // un-revoke the card.
          if (d.userRevoked !== undefined) setRevoked(d.userRevoked === true)
          setRevokedAt(typeof d.revokedAt === "string" ? d.revokedAt : null)
        })
        .catch(() => {})
    }
    read()
    // Authenticating happens in the worker app, in another window, so
    // this card was guaranteed to be stale exactly when it mattered:
    // read once on mount, then never again while the user went away,
    // signed in, and came back to a card still offering "Connect" for a
    // channel the server already considered authenticated.
    //
    // Returning to this window IS the end of that round trip, so it is
    // the right moment to ask again.
    window.addEventListener("focus", read)
    return () => {
      cancelled = true
      window.removeEventListener("focus", read)
    }
  }, [channelId])

  // Both routes answer with the same full status payload, so adopt it
  // rather than guessing. Guessing is how the card ends up disagreeing
  // with the server about what just happened.
  const applyStatus = (data: AuthStatus | null): AuthStatus | null => {
    if (!data || typeof data.authenticated !== "boolean") return null
    setStatus(data)
    if (data.userRevoked !== undefined) setRevoked(data.userRevoked === true)
    setRevokedAt(typeof data.revokedAt === "string" ? data.revokedAt : null)
    return data
  }

  const revoke = async () => {
    if (pending) return
    setPending(true)
    actedForRef.current = channelId
    try {
      const res = await fetch(
        `/api/youtube/channels/${encodeURIComponent(channelId)}/authentication`,
        { method: "DELETE", credentials: "include" }
      )
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = applyStatus(await res.json().catch(() => null))
      if (!data) {
        // The server accepted it, we just couldn't read the reply. It is
        // revoked; only the date shown falls back to our own clock.
        setRevoked(true)
        setRevokedAt(new Date().toISOString())
      }
      setRevokeConfirmOpen(false)
      // No success toast on purpose: the card flips to its disconnected
      // state right in front of the user, and announcing the same fact
      // twice reads as noise. The failure toast below stays - on failure
      // the card does NOT change, so it is the only feedback there is.
    } catch {
      toast({
        title: "Couldn't disconnect this YouTube account",
        description: "Try again in a moment.",
        variant: "error",
      })
    } finally {
      setPending(false)
    }
  }

  const restore = async () => {
    if (pending) return
    setPending(true)
    actedForRef.current = channelId
    try {
      const res = await fetch(
        `/api/youtube/channels/${encodeURIComponent(channelId)}/authentication`,
        { method: "POST", credentials: "include" }
      )
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = applyStatus(await res.json().catch(() => null))
      if (!data) {
        setRevoked(false)
        setRevokedAt(null)
      }
      // No success toast, same rule as the disconnect above: the card
      // flips in front of the user, and the state it flips to carries the
      // next step itself (the Connect button that opens the worker app).
    } catch {
      toast({
        title: "Couldn't allow reconnecting",
        description: "Try again in a moment.",
        variant: "error",
      })
    } finally {
      setPending(false)
    }
  }

  return (
    <section className="border border-border p-4">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">
        Authentication
      </div>
      {/* Revoked is checked ahead of the loading state, not just ahead of
          the authenticated one: the flag is seeded from the channel payload,
          so a known revocation renders straight away instead of sitting on
          "Checking…" while the status fetch flies. */}
      {revoked ? (
        // The user's own choice, not a fault - muted, same as the
        // never-authenticated state, never the destructive red. One row to
        // mirror the authorized state, so the card flips in place. The
        // details the old paragraph carried live where they act: what
        // disconnecting did was in the confirm dialog, and what reconnecting
        // takes is shown by the not-authenticated state this flips to.
        // "Reconnect" only lifts our block - the sign-in happens in the
        // worker app - but the next card state and the toast both say so at
        // the moment it matters, which a longer label could not.
        <div className="flex items-center justify-between gap-4">
          <div className="flex items-center gap-2.5">
            <ShieldOff className="size-5 text-muted-foreground shrink-0" />
            <div className="text-sm font-semibold">
              YouTube account disconnected
              {revokedAt ? (
                <span className="font-normal text-muted-foreground">
                  {" "}
                  · {formatFullDate(revokedAt)}
                </span>
              ) : null}
            </div>
          </div>
          <button
            type="button"
            onClick={() => void restore()}
            disabled={pending}
            className="border-2 border-border text-white font-bold px-4 py-1.5 text-sm cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {pending ? "Reconnecting…" : "Reconnect"}
          </button>
        </div>
      ) : status === null ? (
        <div className="text-xs text-muted-foreground">Checking…</div>
      ) : status.authenticated ? (
        // One row: the heading and its one action. The separate rule-and-
        // button zone existed to keep the action clear of a paragraph of
        // body copy; once that copy went, the zone was fencing off empty
        // space. States the standing grant, not a live session check - the
        // app can be closed or signed out and this still reads true; what
        // the grant unlocks is spelled out where it is actionable (the
        // Disconnect dialog, the not-authenticated state below).
        // "Disconnect" over "Deauthenticate": YouTube's own word for
        // withdrawing an app's access, and what actually happens - the
        // worker signs this account out. Red by the owner's call: in this
        // panel red marks the actions that warrant a pause (this one signs
        // the app out of the account, Remove channel takes the archive
        // away), not only the irreversible one. Nothing is deleted either
        // way, and the dialog says so.
        <div className="flex items-center justify-between gap-4">
          <div className="flex items-center gap-2.5">
            <ShieldCheck className="size-5 text-emerald-400 shrink-0" />
            <div className="text-sm font-semibold">Worker app authorized</div>
          </div>
          <button
            type="button"
            onClick={() => setRevokeConfirmOpen(true)}
            disabled={pending}
            className="border-2 border-destructive text-white font-bold px-4 py-1.5 text-sm cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Disconnect
          </button>
        </div>
      ) : (
        // Same one-row idiom as the other two states. The next step lives
        // in the worker app, so the button opens it (same deep link the
        // Settings Connections card uses) instead of a paragraph describing
        // where to go. If the app is not installed the browser shows its
        // own "nothing handles this" prompt and the toast points at the
        // download - not detectable from JS, same trade Settings makes.
        <div className="flex items-center justify-between gap-4">
          <div className="flex items-center gap-2.5">
            <Lock className="size-5 text-muted-foreground shrink-0" />
            <div className="text-sm font-semibold">Only public videos sync</div>
          </div>
          <button
            type="button"
            onClick={() => {
              window.location.href = "aether-archive-tool://connect-account"
              toast({
                title: "Opening your worker app",
                description:
                  "Sign into this channel's Google account there and private videos unlock here automatically. If nothing happens, install the worker app first (Settings - Worker App).",
              })
            }}
            className="border-2 border-border text-white font-bold px-4 py-1.5 text-sm cursor-pointer whitespace-nowrap"
          >
            Connect
          </button>
        </div>
      )}

      <Dialog
        open={revokeConfirmOpen}
        onOpenChange={(open) => {
          if (!pending) setRevokeConfirmOpen(open)
        }}
      >
        <DialogContent className="max-w-md">
          <div className="p-6 space-y-5">
            <DialogHeader>
              <DialogTitle>Disconnect this YouTube account?</DialogTitle>
            </DialogHeader>
            {/* Every load-bearing fact in one breath, nothing twice: app
                signs out (browser does not), new sealed sync stops, nothing
                deleted, archive still billed, reversible. The billing
                mechanics and the reconnect steps live where they happen -
                the Billing tab and the disconnected-state card. */}
            <p className="text-sm text-muted-foreground leading-relaxed">
              Your worker app signs out of the YouTube account that owns{" "}
              <span className="font-semibold text-foreground">
                {channelName}
              </span>
              , and new private, unlisted, and members-only videos stop
              syncing. Your browser isn't touched.{" "}
              <strong className="text-foreground">Nothing is deleted</strong> -
              everything already archived stays, and stays billed as storage.
              Public videos carry on, and you can reconnect whenever you want.
            </p>
            <DialogFooter>
              <Button
                variant="ghost"
                onClick={() => setRevokeConfirmOpen(false)}
                disabled={pending}
              >
                Cancel
              </Button>
              {/* Destructive to match the card button and the Remove-channel
                  dialog: red is the color of confirm-something-real here. */}
              <Button
                variant="destructive"
                onClick={() => void revoke()}
                disabled={pending}
              >
                <ShieldOff />
                {pending ? "Disconnecting…" : "Disconnect"}
              </Button>
            </DialogFooter>
          </div>
        </DialogContent>
      </Dialog>
    </section>
  )
}

function Section({
  label,
  headerRight,
  children,
}: {
  label: string
  headerRight?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <section>
      <div className="flex items-center justify-between gap-4 mb-3">
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold">
          {label}
        </div>
        {headerRight}
      </div>
      <div className="space-y-3">{children}</div>
    </section>
  )
}

// The "Save" / "History" column labels, aligned above the two switch
// columns of MetadataToggleRow.
function MetadataColumnLabels() {
  return (
    <div className="flex items-center shrink-0 text-[10px] uppercase tracking-wider text-muted-foreground">
      <div className="w-16 text-center">Save</div>
      <div className="w-16 text-center">History</div>
    </div>
  )
}

// Label left, control right on one row - same shape as the toggle rows, so
// the whole panel reads as one column of labels with their controls on the
// right instead of alternating stacked/inline blocks.
function Field({
  label,
  children,
}: {
  label: string
  children: React.ReactNode
}) {
  return (
    <div className="flex items-center gap-4">
      <div className="text-sm font-semibold shrink-0">{label}</div>
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  )
}

function Divider() {
  return <div className="h-px w-full bg-white/60" />
}

function ScopeBreak({ label }: { label: string }) {
  return (
    <div className="pt-4">
      <div className="h-[3px] w-full bg-white" />
      <div className="text-lg font-extrabold tracking-tight mt-4">{label}</div>
    </div>
  )
}

function EstimateBlock({
  resolution,
  codec,
  channelDurationSec,
  measured,
}: {
  resolution: VideoMaxResolution
  codec: CodecPreference
  channelDurationSec: number
  measured?: {
    bytes: number
    durationSec: number
    resolution: VideoMaxResolution
    codec: CodecPreference
  }
}) {
  const prices = usePrices()
  const gbPerHour = projectedGbPerHour({ resolution, codec }, measured)
  const totalGb = (gbPerHour * channelDurationSec) / 3600
  const monthlyCostUsd = estimateMonthlyStorageCostUsd(
    totalGb * 1_000_000_000,
    prices.storagePerGbMonth,
  )

  return (
    <div className="border border-border p-3 mt-1">
      {/* A projection of the WHOLE channel at the quality selected above,
          not what is archived now - that real number lives in the channel
          header. Labelled "if fully synced" so the two never read as
          contradictory: 812 MB archived vs a larger full-catalog estimate
          is expected, not a bug. Anchored to the user's own bytes-per-hour
          via projectedGbPerHour so the estimate tracks reality. */}
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-2">
        If fully synced
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-1.5">
            Est. size
          </div>
          <div className="text-base font-bold font-mono tabular-nums">
            ~{formatGb(totalGb)}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-1.5">
            Est. cost
          </div>
          <div className="text-base font-bold font-mono tabular-nums">
            {monthlyCostUsd < 0.01
              ? "<$0.01"
              : `~$${monthlyCostUsd.toFixed(2)}`}
            <span className="text-muted-foreground font-normal">/mo</span>
          </div>
        </div>
      </div>
    </div>
  )
}

function ToggleRow({
  label,
  description,
  checked,
  disabled,
  onChange,
}: {
  label: string
  description?: string
  checked: boolean
  disabled?: boolean
  onChange?: (v: boolean) => void
}) {
  return (
    <div className="flex items-center justify-between gap-4">
      <div className="min-w-0 flex-1">
        <div
          className={cn(
            "text-sm font-semibold",
            disabled && "text-muted-foreground"
          )}
        >
          {label}
        </div>
        {description && (
          <div className="text-xs text-muted-foreground mt-0.5 leading-relaxed">
            {description}
          </div>
        )}
      </div>
      <Switch
        checked={checked}
        onCheckedChange={onChange ?? (() => {})}
        disabled={disabled}
        aria-label={label}
      />
    </div>
  )
}

// A metadata field with two independent switches: "Save" (capture the
// current value) and "History" (keep prior values as a change-log / graph).
// History requires capture, so it disables when Save is off. Fields without
// history support (onHistoryChange omitted) show a dash in the History slot.
function MetadataToggleRow({
  label,
  captured,
  onCapturedChange,
  history,
  onHistoryChange,
}: {
  label: string
  captured: boolean
  onCapturedChange: (v: boolean) => void
  history?: boolean
  onHistoryChange?: (v: boolean) => void
}) {
  return (
    <div className="flex items-center gap-4">
      <div className="min-w-0 flex-1 text-sm font-semibold">{label}</div>
      <div className="flex items-center shrink-0">
        <div className="w-16 flex justify-center">
          <Switch
            checked={captured}
            onCheckedChange={onCapturedChange}
            aria-label={`${label} save`}
          />
        </div>
        <div className="w-16 flex justify-center">
          {onHistoryChange && (
            <Switch
              checked={captured && !!history}
              onCheckedChange={onHistoryChange}
              disabled={!captured}
              aria-label={`${label} history`}
            />
          )}
        </div>
      </div>
    </div>
  )
}
