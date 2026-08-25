import { Link } from "react-router-dom"
import { ArrowLeft } from "lucide-react"
import { Button } from "@/components/ui/button"
import { EmptyState } from "@/components/EmptyState"

export default function NotFound() {
  return (
    <div className="p-8">
      <EmptyState
        title="Page not found"
        description="That route doesn't exist."
        action={
          <Button asChild variant="outline">
            <Link to="/">
              <ArrowLeft />
              Back to dashboard
            </Link>
          </Button>
        }
      />
    </div>
  )
}
