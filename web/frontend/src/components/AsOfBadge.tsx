import { Clock3 } from 'lucide-react'

import { cn } from '../lib/utils'
import { formatJstStamp } from '../lib/format'
import { LABEL } from '../lib/labels'

interface AsOfBadgeProps {
  value: string | null
  compact?: boolean
  className?: string
}

// The one way this app states "what moment is this number from". A value may carry a
// time of day (a price observed at the close) or not (a reading computed for a date);
// showing 00:00 for the second kind would invent a precision the value does not have.
export function AsOfBadge({ value, compact = false, className }: AsOfBadgeProps) {
  if (value === null) return <span className={cn('text-xs text-muted-foreground', className)}>{LABEL.asOf} —</span>
  const rendered = formatJstStamp(value)
  return (
    <time className={cn('inline-flex items-center gap-1 font-mono text-xs tabular-nums text-muted-foreground', className)} dateTime={value}>
      {!compact && <Clock3 className="size-3.5" aria-hidden="true" />}
      {compact ? rendered : `${LABEL.asOf} ${rendered}`}
    </time>
  )
}
