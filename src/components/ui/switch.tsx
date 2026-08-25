import { cn } from "@/lib/utils"

type Props = {
  checked: boolean
  onCheckedChange: (checked: boolean) => void
  disabled?: boolean
  "aria-label"?: string
  id?: string
}

export function Switch({
  checked,
  onCheckedChange,
  disabled,
  id,
  "aria-label": ariaLabel,
}: Props) {
  return (
    <button
      type="button"
      role="switch"
      id={id}
      aria-checked={checked}
      aria-label={ariaLabel}
      disabled={disabled}
      onClick={() => onCheckedChange(!checked)}
      className={cn(
        "relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center border-2 border-white outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:opacity-50 disabled:cursor-not-allowed",
        checked ? "bg-white" : "bg-transparent"
      )}
    >
      <span
        className={cn(
          "pointer-events-none inline-block h-3 w-3 transform",
          checked ? "bg-black translate-x-[18px]" : "bg-white translate-x-[2px]"
        )}
      />
    </button>
  )
}
