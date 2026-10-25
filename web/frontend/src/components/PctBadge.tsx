import { cn } from '../lib/utils'
import { EMPTY, formatPct } from '../lib/format'
import { pnlTone } from '../lib/portfolio'

interface PctBadgeProps {
  value: number | null
  fraction?: boolean
  // `pnl` for a gain or loss on money, the default for the direction of any other metric.
  tone?: 'metric' | 'pnl'
  className?: string
}

export function PctBadge({ value, fraction = false, tone = 'metric', className }: PctBadgeProps) {
  if (value === null) return <span className={cn('text-muted-foreground', className)}>{EMPTY}</span>
  const percentage = fraction ? value * 100 : value
  return (
    <span className={cn(
      'font-mono font-medium tabular-nums',
      tone === 'pnl' ? pnlTone(percentage) : [
        percentage > 0 && 'text-positive',
        percentage < 0 && 'text-destructive',
        percentage === 0 && 'text-muted-foreground',
      ],
      className,
    )}>
      {formatPct(value, { fraction, sign: true })}
    </span>
  )
}
