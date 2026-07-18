interface PctBadgeProps {
  value: number | null
  fraction?: boolean
}

export function PctBadge({ value, fraction = false }: PctBadgeProps) {
  if (value === null) return <span className="muted">—</span>
  const percentage = fraction ? value * 100 : value
  const tone = percentage > 0 ? 'positive' : percentage < 0 ? 'negative' : 'neutral'
  const sign = percentage > 0 ? '+' : ''
  return <span className={`pct pct--${tone}`}>{sign}{percentage.toFixed(1)}%</span>
}
