import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "@/lib/utils"

const badgeVariants = cva(
  // Dark backdrop + blur on every badge so the colored text stays readable
  // when stacked over busy YouTube thumbnails. Each variant just sets text
  // color (and optionally a border).
  "inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-semibold gap-1 [&_svg]:size-3 [&_svg]:shrink-0 bg-black/80 backdrop-blur-sm",
  {
    variants: {
      variant: {
        default: "border-transparent text-primary",
        // Secondary is for chrome (sidebar pills etc), not thumbnails — keep
        // the solid theme background so it doesn't read as a transparent overlay.
        secondary: "border-transparent bg-secondary text-secondary-foreground",
        outline: "border-border text-foreground",
        destructive: "border-transparent text-red-400",
        success: "border-transparent text-emerald-400",
        warning: "border-amber-400 text-amber-400",
        private: "border-red-400 text-red-400",
        members: "border-emerald-400 text-emerald-400",
        deleted: "border-blue-400 text-blue-400",
      },
    },
    defaultVariants: { variant: "default" },
  }
)

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <div className={cn(badgeVariants({ variant }), className)} {...props} />
}

export { badgeVariants }
