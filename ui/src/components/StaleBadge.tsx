import { Badge } from './ui/badge'
import { cn } from '../lib/utils'

interface StaleBadgeProps {
  // Optional qualifier appended after `stale —` (e.g. the as-of date).
  detail?: string
  className?: string
}

// The one stale indicator across Baibai Loop, styled with the semantic warning token.
export function StaleBadge({ detail, className }: StaleBadgeProps) {
  return (
    <Badge className={cn('border-warning/50 text-warning', className)} variant="outline">
      stale{detail !== undefined && ` — ${detail}`}
    </Badge>
  )
}
