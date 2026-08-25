import * as React from "react"
import {
  ArrowDown,
  ArrowUp,
  ChevronDown,
  LayoutGrid,
  LayoutList,
  Trash2,
} from "lucide-react"
import { Button } from "./ui/button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "./ui/select"
import type {
  FilterPreset,
  SortDimension,
  VideoType,
  VideoVisibility,
} from "@/lib/types"
import { cn } from "@/lib/utils"

export const VISIBILITY_OPTIONS: VideoVisibility[] = [
  "public",
  "unlisted",
  "private",
  "members",
  "deleted",
]

/**
 * Display labels for the visibility values. The stored value and the label
 * deliberately differ for "deleted": saved filter presets persist
 * visibilities:["deleted"] in the database, so the value can never change,
 * but "Deleted" names an actor we cannot identify. Our detection collapses
 * takedowns, TOS removals, terminated accounts, region blocks, age gates and
 * scraper bot-checks into one indistinguishable state. The only fact we have
 * is first-person: we could not see the video when we last looked.
 * "Unavailable" says exactly that, stays true if the video comes back, and
 * does not collide with "Recently removed" on the channels page (which means
 * the user removed a channel from their own list).
 */
export const VISIBILITY_LABELS: Record<VideoVisibility, string> = {
  public: "Public",
  unlisted: "Unlisted",
  private: "Private",
  members: "Members",
  deleted: "Unavailable",
}

export const TYPE_OPTIONS: { value: VideoType; label: string }[] = [
  { value: "video", label: "Video" },
  { value: "short", label: "Short" },
  { value: "livestream", label: "Livestream" },
]

export const SORT_DIMENSION_LABELS: Record<SortDimension, string> = {
  upload: "Upload date",
  views: "Views",
  filesize: "File size",
  duration: "Duration",
}

export function generatePresetId(): string {
  return `preset-${Math.random().toString(36).slice(2, 10)}`
}

type Props = {
  preset: FilterPreset
  onUpdate: (next: FilterPreset) => void
  onDelete: () => void
}

export function PresetEditor({ preset, onUpdate, onDelete }: Props) {
  const [expanded, setExpanded] = React.useState(false)
  const update = <K extends keyof FilterPreset>(
    key: K,
    value: FilterPreset[K]
  ) => onUpdate({ ...preset, [key]: value })

  const toggleVisibility = (v: VideoVisibility) => {
    update(
      "visibilities",
      preset.visibilities.includes(v)
        ? preset.visibilities.filter((x) => x !== v)
        : [...preset.visibilities, v]
    )
  }

  const toggleType = (t: VideoType) => {
    update(
      "types",
      preset.types.includes(t)
        ? preset.types.filter((x) => x !== t)
        : [...preset.types, t]
    )
  }

  return (
    <div className="border border-border">
      <div className="flex items-center gap-2 p-2">
        <button
          type="button"
          onClick={() => setExpanded((e) => !e)}
          className="flex-1 flex items-center gap-2 text-left cursor-pointer"
          aria-expanded={expanded}
        >
          <ChevronDown
            className={cn(
              "size-4 text-muted-foreground",
              expanded && "rotate-180"
            )}
          />
          <span className="font-semibold text-sm">{preset.label}</span>
        </button>
        {!preset.locked && (
          <button
            type="button"
            onClick={onDelete}
            aria-label={`Delete ${preset.label} preset`}
            className="size-7 flex items-center justify-center text-muted-foreground cursor-pointer"
          >
            <Trash2 className="size-4" />
          </button>
        )}
      </div>

      {expanded && (
        <div className="p-3 border-t border-border space-y-3">
          {!preset.locked && (
            <>
              <FieldRow label="Label">
                <input
                  type="text"
                  value={preset.label}
                  onChange={(e) => update("label", e.target.value)}
                  className="h-9 w-full border border-border bg-transparent px-3 text-sm text-foreground outline-none focus:border-white focus:bg-white/5"
                />
              </FieldRow>

              <FieldRow label="Search text">
                <input
                  type="text"
                  value={preset.search}
                  onChange={(e) => update("search", e.target.value)}
                  placeholder="(none)"
                  className="h-9 w-full border border-border bg-transparent px-3 text-sm text-foreground outline-none focus:border-white focus:bg-white/5 placeholder:text-muted-foreground"
                />
              </FieldRow>

              <FieldRow label="Visibility">
                <div className="flex flex-wrap gap-1.5">
                  {VISIBILITY_OPTIONS.map((v) => {
                    const active = preset.visibilities.includes(v)
                    return (
                      <button
                        key={v}
                        type="button"
                        onClick={() => toggleVisibility(v)}
                        className={cn(
                          "px-2.5 py-1 text-xs font-semibold border cursor-pointer",
                          active
                            ? "bg-white text-black border-white"
                            : "border-border text-foreground"
                        )}
                      >
                        {VISIBILITY_LABELS[v]}
                      </button>
                    )
                  })}
                </div>
              </FieldRow>

              <FieldRow label="Type">
                <div className="flex flex-wrap gap-1.5">
                  {TYPE_OPTIONS.map((o) => {
                    const active = preset.types.includes(o.value)
                    return (
                      <button
                        key={o.value}
                        type="button"
                        onClick={() => toggleType(o.value)}
                        className={cn(
                          "px-2.5 py-1 text-xs font-semibold border cursor-pointer",
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
              </FieldRow>

              <FieldRow label="Upload date">
                <div className="grid grid-cols-2 gap-2">
                  <input
                    type="date"
                    value={preset.dateFrom}
                    onChange={(e) => update("dateFrom", e.target.value)}
                    className="h-9 w-full border border-border bg-transparent px-2 text-sm text-foreground outline-none focus:border-white [color-scheme:dark]"
                  />
                  <input
                    type="date"
                    value={preset.dateTo}
                    onChange={(e) => update("dateTo", e.target.value)}
                    className="h-9 w-full border border-border bg-transparent px-2 text-sm text-foreground outline-none focus:border-white [color-scheme:dark]"
                  />
                </div>
              </FieldRow>
            </>
          )}

          <FieldRow label="Sort">
            <div className="flex items-center gap-2">
              <Select
                value={preset.sortDimension}
                onValueChange={(v) =>
                  update("sortDimension", v as SortDimension)
                }
              >
                <SelectTrigger className="flex-1">
                  <SelectValue>
                    {SORT_DIMENSION_LABELS[preset.sortDimension]}
                  </SelectValue>
                </SelectTrigger>
                <SelectContent>
                  {(Object.keys(SORT_DIMENSION_LABELS) as SortDimension[]).map(
                    (k) => (
                      <SelectItem key={k} value={k}>
                        {SORT_DIMENSION_LABELS[k]}
                      </SelectItem>
                    )
                  )}
                </SelectContent>
              </Select>
              <Button
                variant="outline"
                size="icon"
                onClick={() =>
                  update(
                    "sortDirection",
                    preset.sortDirection === "asc" ? "desc" : "asc"
                  )
                }
                aria-label={
                  preset.sortDirection === "asc" ? "Ascending" : "Descending"
                }
              >
                {preset.sortDirection === "asc" ? <ArrowUp /> : <ArrowDown />}
              </Button>
            </div>
          </FieldRow>

          <FieldRow label="View">
            <div className="flex">
              <button
                type="button"
                onClick={() => update("viewMode", "grid")}
                aria-label="Grid view"
                aria-pressed={preset.viewMode === "grid"}
                className={cn(
                  "h-9 px-3 flex items-center justify-center gap-2 border text-sm font-semibold cursor-pointer",
                  preset.viewMode === "grid"
                    ? "bg-white text-black border-white"
                    : "border-border text-foreground"
                )}
              >
                <LayoutGrid className="size-4" />
                Grid
              </button>
              <button
                type="button"
                onClick={() => update("viewMode", "list")}
                aria-label="List view"
                aria-pressed={preset.viewMode === "list"}
                className={cn(
                  "h-9 px-3 flex items-center justify-center gap-2 border border-l-0 text-sm font-semibold cursor-pointer",
                  preset.viewMode === "list"
                    ? "bg-white text-black border-white"
                    : "border-border text-foreground"
                )}
              >
                <LayoutList className="size-4" />
                List
              </button>
            </div>
          </FieldRow>
        </div>
      )}
    </div>
  )
}

function FieldRow({
  label,
  children,
}: {
  label: string
  children: React.ReactNode
}) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-1.5">
        {label}
      </div>
      {children}
    </div>
  )
}
