/**
 * ARCHIVE336 — primary logo as a React component.
 *
 * Uses `currentColor` for fill + stroke so the logo inherits whatever
 * text color is on the parent (muted in inactive nav state, full
 * foreground when active). Same geometry as
 * /public/logos/logo.svg — keep them in sync if either changes.
 */
export function Logo({ className }: { className?: string }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 200 200"
      className={className}
      aria-hidden
    >
      <polygon
        points="100,18 170,61 170,139 100,182 30,139 30,61"
        fill="none"
        stroke="currentColor"
        strokeWidth="14"
        strokeLinejoin="miter"
      />
      <rect x="46" y="73" width="108" height="14" fill="currentColor" />
      <rect x="60" y="93" width="80" height="14" fill="currentColor" />
      <rect x="76" y="113" width="48" height="14" fill="currentColor" />
    </svg>
  )
}
