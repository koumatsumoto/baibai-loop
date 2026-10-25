import { cn } from '../lib/utils'
import { EMPTY, formatYen } from '../lib/format'
import { pnlTone } from '../lib/portfolio'

interface YenAmountProps {
  value: number | null
  sign?: boolean
  // `pnl` colors the amount as a gain or a loss; the default leaves it in the inherited
  // text color, which is what a plain balance wants.
  tone?: 'plain' | 'pnl'
  className?: string
}

export function YenAmount({ value, sign = false, tone = 'plain', className }: YenAmountProps) {
  if (value === null) return <span className={cn('text-muted-foreground', className)}>{EMPTY}</span>
  const prefix = sign && value > 0 ? '+' : ''
  return (
    <span className={cn('font-mono tabular-nums', tone === 'pnl' && pnlTone(value), className)}>
      {prefix}{formatYen(value)}
    </span>
  )
}
