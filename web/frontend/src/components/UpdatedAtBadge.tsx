import { Clock3 } from 'lucide-react'

import { formatJstStamp } from '../lib/format'
import { LABEL } from '../lib/labels'
import { cn } from '../lib/utils'

interface UpdatedAtBadgeProps {
  value: string | null
  compact?: boolean
  className?: string
}

// Publication and run timestamps only. Callers must not substitute an as-of,
// observation date, generated_at, or file timestamp when the actual update is absent.
export function UpdatedAtBadge({ value, compact = false, className }: UpdatedAtBadgeProps) {
  if (value === null) {
    return <span className={cn('text-xs text-muted-foreground', className)}>{LABEL.updated} —</span>
  }
  const rendered = formatJstStamp(value)
  return (
    <time className={cn('inline-flex items-center gap-1 font-mono text-xs tabular-nums text-muted-foreground', className)} dateTime={value}>
      {!compact && <Clock3 aria-hidden="true" className="size-3.5" />}
      {LABEL.updated} {rendered}
    </time>
  )
}
