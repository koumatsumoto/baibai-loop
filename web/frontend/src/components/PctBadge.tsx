import { cn } from '../lib/utils'
import { EMPTY, formatPct } from '../lib/format'
import { pnlTone } from '../lib/portfolio'

interface PctBadgeProps {
  value: number | null
  tone?: 'plain' | 'pnl'
  className?: string
}

export function PctBadge({ value, tone = 'plain', className }: PctBadgeProps) {
  if (value === null) return <span className={cn('text-muted-foreground', className)}>{EMPTY}</span>
  return (
    <span className={cn('font-mono font-medium tabular-nums', tone === 'pnl' && pnlTone(value), className)}>
      {formatPct(value, { sign: true })}
    </span>
  )
}
