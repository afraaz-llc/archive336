import * as React from "react"
import { Switch } from "@/components/ui/switch"

/**
 * The light-mode switch, which does not work and never will.
 *
 * A deliberate joke, not an unfinished feature - the distinction matters
 * because the rule everywhere else in this app is that a visible control
 * must be wired to something real. This one is exempt on the grounds
 * that it is honest: it refuses on the first click and says so, so
 * nobody is ever left believing it did something. Please do not "fix" it.
 *
 * No motion, per the rest of the app. Joke toggles usually lean on the
 * switch sliding back or running from the cursor, which is a tween and a
 * tween is a house style violation. This one escalates through text
 * instead, which is funnier at a keyboard anyway and reads the same to a
 * screen reader.
 */
const REFUSALS = [
  "No.",
  "Still no.",
  "This switch isn't connected to anything.",
  "It has never been connected to anything.",
  "We built the switch first and lost interest.",
  "Six clicks. We are both watching this happen.",
  "Light mode was discussed once. It did not go well.",
  "The archive is dark. That is more or less the whole idea.",
  "Have you considered turning your brightness up.",
  "Fine. Squint.",
] as const

export function AppearanceToggle() {
  // -1 = untouched, so the row starts clean rather than pre-refusing
  // something nobody asked for yet.
  const [clicks, setClicks] = React.useState(-1)
  const message = clicks >= 0 ? REFUSALS[Math.min(clicks, REFUSALS.length - 1)] : null

  return (
    <section>
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">
        Appearance
      </div>
      <div className="flex items-center gap-3 border border-border p-3">
        <div className="min-w-0 flex-1">
          <div className="text-sm">Light mode</div>
          {message && (
            // role=status so the refusal is announced. Without it the
            // control reports "off" forever and the joke reads as a
            // broken switch to anyone not looking at the screen.
            <div
              role="status"
              className="text-xs text-muted-foreground mt-1"
            >
              {message}
            </div>
          )}
        </div>
        <Switch
          checked={false}
          onCheckedChange={() => setClicks((n) => n + 1)}
          aria-label="Light mode"
        />
      </div>
    </section>
  )
}
