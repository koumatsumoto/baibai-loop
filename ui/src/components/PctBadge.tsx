import { cn } from '../lib/utils'

interface PctBadgeProps {
  value: number | null
  fraction?: boolean
  className?: string
}

export function PctBadge({ value, fraction = false, className }: PctBadgeProps) {
  if (value === null) return <span className={cn('text-muted-foreground', className)}>—</span>
  const percentage = fraction ? value * 100 : value
  const sign = percentage > 0 ? '+' : ''
  return (
    <span className={cn(
      'font-mono font-medium tabular-nums',
      percentage > 0 && 'text-positive',
      percentage < 0 && 'text-destructive',
      percentage === 0 && 'text-muted-foreground',
      className,
    )}>
      {sign}{percentage.toFixed(1)}%
    </span>
  )
}
