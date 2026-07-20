import { Clock3 } from 'lucide-react'

import { cn } from '../lib/utils'
import { formatJstStamp } from '../lib/format'
import { LABEL } from '../lib/labels'

interface AsOfBadgeProps {
  value: string | null
  compact?: boolean
  className?: string
}

export function AsOfBadge({ value, compact = false, className }: AsOfBadgeProps) {
  if (value === null) return <span className={cn('text-xs text-muted-foreground', className)}>{LABEL.asOf} —</span>
  const rendered = formatJstStamp(value)
  return (
    <span className={cn('inline-flex items-center gap-1 font-mono text-xs tabular-nums text-muted-foreground', className)}>
      {!compact && <Clock3 className="size-3.5" aria-hidden="true" />}
      {compact ? rendered : `${LABEL.asOf} ${rendered}`}
    </span>
  )
}
