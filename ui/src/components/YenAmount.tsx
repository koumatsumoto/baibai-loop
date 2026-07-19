import { cn } from '../lib/utils'

interface YenAmountProps {
  value: number | null
  sign?: boolean
  className?: string
}

const yenFormatter = new Intl.NumberFormat('ja-JP', {
  style: 'currency',
  currency: 'JPY',
  maximumFractionDigits: 0,
})

export function YenAmount({ value, sign = false, className }: YenAmountProps) {
  if (value === null) return <span className={cn('text-muted-foreground', className)}>—</span>
  const prefix = sign && value > 0 ? '+' : ''
  return <span className={cn('font-mono tabular-nums', className)}>{prefix}{yenFormatter.format(value)}</span>
}
