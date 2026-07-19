import { Clock3 } from 'lucide-react'

import { cn } from '../lib/utils'

interface AsOfBadgeProps {
  value: string | null
  stale?: boolean
  compact?: boolean
  className?: string
}

const dateTimeFormatter = new Intl.DateTimeFormat('ja-JP', {
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
})

export function AsOfBadge({ value, stale = false, compact = false, className }: AsOfBadgeProps) {
  if (value === null) return <span className={cn('text-xs text-muted-foreground', className)}>as of —</span>
  const rendered = dateTimeFormatter.format(new Date(value))
  return (
    <span className={cn('inline-flex items-center gap-1 font-mono text-xs tabular-nums text-muted-foreground', stale && 'text-destructive', className)}>
      {!compact && <Clock3 className="size-3.5" aria-hidden="true" />}
      {compact ? rendered : `as of ${rendered}`}
      {stale && <span className="font-sans">（stale）</span>}
    </span>
  )
}
