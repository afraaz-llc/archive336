import * as React from "react"
import { X } from "lucide-react"
import { cn } from "@/lib/utils"

type ToastVariant = "default" | "success" | "error"
type Toast = {
  id: string
  title: string
  description?: string
  variant?: ToastVariant
  duration?: number
}

type ToastCtxValue = {
  toast: (t: Omit<Toast, "id">) => void
}

const DEFAULT_DURATION_MS = 10_000

const ToastCtx = React.createContext<ToastCtxValue | null>(null)

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = React.useState<Toast[]>([])

  const toast = React.useCallback((t: Omit<Toast, "id">) => {
    const id = Math.random().toString(36).slice(2)
    setToasts((curr) => [...curr, { ...t, id }])
    window.setTimeout(() => {
      setToasts((curr) => curr.filter((x) => x.id !== id))
    }, t.duration ?? DEFAULT_DURATION_MS)
  }, [])

  const dismiss = (id: string) => setToasts((curr) => curr.filter((x) => x.id !== id))

  return (
    <ToastCtx.Provider value={{ toast }}>
      {children}
      <div className="fixed bottom-4 right-4 z-[100] flex w-80 flex-col gap-2 pointer-events-none">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={cn(
              "pointer-events-auto relative border bg-card p-4 pr-10 shadow-xl",
              t.variant === "error" && "border-destructive/50",
              t.variant === "success" && "border-emerald-500/40"
            )}
          >
            <div className="text-sm font-medium leading-snug">{t.title}</div>
            {t.description && (
              <div className="mt-1 text-xs text-muted-foreground leading-relaxed">
                {t.description}
              </div>
            )}
            <button
              type="button"
              onClick={() => dismiss(t.id)}
              className="absolute right-2 top-2 rounded p-1 text-muted-foreground cursor-pointer"
              aria-label="Dismiss"
            >
              <X className="size-3.5" />
            </button>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  )
}

export function useToast() {
  const ctx = React.useContext(ToastCtx)
  if (!ctx) throw new Error("useToast must be used within <ToastProvider>")
  return ctx
}
