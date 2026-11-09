import { cn } from '../lib/utils'
import { EMPTY, formatYen } from '../lib/format'
import { pnlTone } from '../lib/portfolio'

interface YenAmountProps {
  value: number | null
  sign?: boolean
  // Only profit and loss amounts use gain/loss colors.
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
