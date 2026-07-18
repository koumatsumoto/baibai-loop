interface AsOfBadgeProps {
  value: string | null
  stale?: boolean
  compact?: boolean
}

const dateTimeFormatter = new Intl.DateTimeFormat('ja-JP', {
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
})

export function AsOfBadge({ value, stale = false, compact = false }: AsOfBadgeProps) {
  if (value === null) return <span className="asof asof--empty">as of —</span>
  const rendered = dateTimeFormatter.format(new Date(value))
  return (
    <span className={`asof${stale ? ' asof--stale' : ''}`}>
      {compact ? rendered : `as of ${rendered}`}
    </span>
  )
}
