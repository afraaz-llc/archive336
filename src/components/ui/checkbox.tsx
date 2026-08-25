import { Check } from "lucide-react"
import { cn } from "@/lib/utils"

type Props = {
  checked: boolean
  onCheckedChange: (checked: boolean) => void
  disabled?: boolean
  id?: string
  "aria-label"?: string
}

/**
 * Square checkbox styled to match the rest of the app: white border,
 * sharp corners, white fill + black check when on. Use for opt-ins
 * ('email me X') where Switch doesn't fit semantically.
 *
 * Wrap with a <label> if you want the label text to also toggle.
 */
export function Checkbox({
  checked,
  onCheckedChange,
  disabled,
  id,
  "aria-label": ariaLabel,
}: Props) {
  return (
    <button
      type="button"
      role="checkbox"
      id={id}
      aria-checked={checked}
      aria-label={ariaLabel}
      disabled={disabled}
      onClick={() => onCheckedChange(!checked)}
      className={cn(
        "relative inline-flex size-4 shrink-0 items-center justify-center border-2 border-white outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer",
        checked ? "bg-white" : "bg-transparent"
      )}
    >
      {checked && <Check className="size-3 text-black" strokeWidth={3} />}
    </button>
  )
}
