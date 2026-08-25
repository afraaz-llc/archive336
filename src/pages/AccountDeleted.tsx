import { Link } from "react-router-dom"

/**
 * Public 'your account has been deleted' landing page. The receipt
 * email points here, and the DangerZone delete flow redirects here
 * after a successful deletion.
 *
 * No backend involvement: by the time anyone reaches this URL the
 * deletion has already happened.
 */
export default function AccountDeleted() {
  return (
    <div className="min-h-screen bg-background text-foreground px-6 flex items-center justify-center">
      <div className="w-full max-w-md text-center space-y-6">
        <h1 className="text-4xl font-extrabold tracking-tight mb-12">
          ARCHIVE336
        </h1>

        <h2 className="text-xl font-bold">Your account has been deleted</h2>
        <Link
          to="/auth"
          className="inline-block text-xs uppercase tracking-wider text-muted-foreground font-semibold hover:text-foreground"
        >
          Sign up →
        </Link>
      </div>
    </div>
  )
}
