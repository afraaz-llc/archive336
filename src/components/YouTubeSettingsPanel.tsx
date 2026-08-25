import * as React from "react"
import { Plus } from "lucide-react"
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetBody,
  SheetFooter,
} from "./ui/sheet"
import { Button } from "./ui/button"
import { Switch } from "./ui/switch"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "./ui/select"
import type {
  ChannelArchiveSettings,
  CodecPreference,
  FilterPreset,
  MetadataRefreshFrequency,
  VideoCardMetaField,
  VideoMaxResolution,
} from "@/lib/types"
import { cn } from "@/lib/utils"
import { PresetEditor, generatePresetId } from "./PresetEditor"

type Props = {
  settings: ChannelArchiveSettings
  open: boolean
  onOpenChange: (open: boolean) => void
  onSave: (settings: ChannelArchiveSettings) => void
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

export function YouTubeSettingsPanel({
  settings,
  open,
  onOpenChange,
  onSave,
}: Props) {
  const [draft, setDraft] = React.useState<ChannelArchiveSettings>(settings)

  React.useEffect(() => {
    if (open) setDraft(settings)
  }, [open, settings])

  const dirty = JSON.stringify(draft) !== JSON.stringify(settings)

  const handleSave = () => {
    onSave(draft)
    onOpenChange(false)
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent>
        <SheetHeader>
          <SheetTitle>YouTube settings</SheetTitle>
        </SheetHeader>

        <SheetBody className="space-y-6">
          <div className="flex items-center justify-between gap-4">
            <div className="text-lg font-extrabold tracking-tight">
              New channel defaults
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <span className="text-sm font-semibold">Active</span>
              <Switch
                checked={draft.active}
                onCheckedChange={(v) => setDraft({ ...draft, active: v })}
                aria-label="Active"
              />
            </div>
          </div>

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
            {/* Account-level defaults, so there is no single channel's auth
                to check here. Comment sync needs the channel authenticated:
                either a web-OAuth Google link (Creator+) or, for Basic, the
                worker app signed into the account - the worker fetches
                comments the same way it fetches private videos. The
                per-channel panel disables the control outright when a
                specific channel isn't authenticated. */}
            <ToggleRow
              label="Sync comments"
              description="Applies to channels you've authenticated in the worker app"
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

          <ScopeHeader label="Display settings" />

          <Section label="Video previews">
            <ToggleRow
              label="Status badge"
              checked={draft.showStatusBadges}
              onChange={(v) => setDraft({ ...draft, showStatusBadges: v })}
            />
            <ToggleRow
              label="Status border"
              checked={draft.useStatusColorBorder}
              onChange={(v) => setDraft({ ...draft, useStatusColorBorder: v })}
            />
            <div>
              <div className="text-sm font-semibold mb-2">Meta fields</div>
              <div className="flex flex-wrap gap-1.5">
                {META_FIELD_OPTIONS.map((o) => {
                  const active = draft.cardMetaFields.includes(o.value)
                  return (
                    <button
                      key={o.value}
                      type="button"
                      onClick={() => {
                        setDraft({
                          ...draft,
                          cardMetaFields: active
                            ? draft.cardMetaFields.filter((f) => f !== o.value)
                            : [...draft.cardMetaFields, o.value],
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
              {draft.filterPresets.map((preset, idx) => (
                <PresetEditor
                  key={preset.id}
                  preset={preset}
                  onUpdate={(updated) => {
                    const next = [...draft.filterPresets]
                    next[idx] = updated
                    setDraft({ ...draft, filterPresets: next })
                  }}
                  onDelete={() => {
                    setDraft({
                      ...draft,
                      filterPresets: draft.filterPresets.filter(
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
                setDraft({
                  ...draft,
                  filterPresets: [...draft.filterPresets, newPreset],
                })
              }}
              className="w-full border border-dashed border-border px-3 py-2 text-sm font-semibold text-muted-foreground cursor-pointer flex items-center justify-center gap-2 mt-3"
            >
              <Plus className="size-4" /> Add preset
            </button>
          </Section>

          <ScopeHeader label="Notifications" />

          <Section label="Archive integrity">
            {/*
              We only detect that a video stopped being visible on our sync
              pass. We cannot tell whether it was deleted, made private, or
              pulled by YouTube, so the copy promises visibility only.
              "Track", not "archive": this fires on any video row that flips
              to unavailable, including ones we only ever discovered and never
              downloaded, so claiming we hold a copy would sometimes be false.
            */}
            <ToggleRow
              label="Video no longer available"
              checked={draft.notifyVideoDeleted}
              onChange={(v) => setDraft({ ...draft, notifyVideoDeleted: v })}
            />
            {/*
              Same caveat as the badge on ChannelCard: this alert fires off a
              two-strike scrape failure, which cannot distinguish a ban from a
              rate-limit. Do not name a cause we did not observe.
            */}
            <ToggleRow
              label="Channel no longer available"
              checked={draft.notifyChannelTerminated}
              onChange={(v) =>
                setDraft({ ...draft, notifyChannelTerminated: v })
              }
            />
            <ToggleRow
              label="OAuth disconnected"
              checked={draft.notifyOauthDisconnected}
              onChange={(v) =>
                setDraft({ ...draft, notifyOauthDisconnected: v })
              }
            />
          </Section>

          <Divider />

          <Section label="Activity">
            <ToggleRow
              label="New upload from a tracked channel"
              checked={draft.notifyNewUpload}
              onChange={(v) => setDraft({ ...draft, notifyNewUpload: v })}
            />
            <ToggleRow
              label="Monthly archive digest"
              checked={draft.notifyMonthlyDigest}
              onChange={(v) => setDraft({ ...draft, notifyMonthlyDigest: v })}
            />
          </Section>
        </SheetBody>

        <SheetFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={handleSave} disabled={!dirty}>
            Save changes
          </Button>
        </SheetFooter>
      </SheetContent>
    </Sheet>
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

// The "Save" / "History" column labels, aligned to sit above the two
// switch columns of MetadataToggleRow. Used inline in a Section header
// (Channel info) or as a standalone row (Metadata sync, below its dropdown).
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

function ScopeHeader({ label, first }: { label: string; first?: boolean }) {
  return (
    <div className={first ? "" : "pt-2"}>
      {!first && <div className="h-[3px] w-full bg-white" />}
      <div className={cn("text-lg font-extrabold tracking-tight", !first && "mt-4")}>
        {label}
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
          className={
            "text-sm font-semibold " +
            (disabled ? "text-muted-foreground" : "")
          }
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
