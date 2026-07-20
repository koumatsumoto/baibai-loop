import { cn } from '../lib/utils'
import { EMPTY, formatYen } from '../lib/format'

interface YenAmountProps {
  value: number | null
  sign?: boolean
  className?: string
}

export function YenAmount({ value, sign = false, className }: YenAmountProps) {
  if (value === null) return <span className={cn('text-muted-foreground', className)}>{EMPTY}</span>
  const prefix = sign && value > 0 ? '+' : ''
  return <span className={cn('font-mono tabular-nums', className)}>{prefix}{formatYen(value)}</span>
}
