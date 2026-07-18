interface YenAmountProps {
  value: number | null
  sign?: boolean
}

const yenFormatter = new Intl.NumberFormat('ja-JP', {
  style: 'currency',
  currency: 'JPY',
  maximumFractionDigits: 0,
})

export function YenAmount({ value, sign = false }: YenAmountProps) {
  if (value === null) return <span className="muted">—</span>
  const prefix = sign && value > 0 ? '+' : ''
  return <span className="numeric">{prefix}{yenFormatter.format(value)}</span>
}
