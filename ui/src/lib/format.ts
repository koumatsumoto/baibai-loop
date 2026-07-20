// Single source for how the cockpit renders values. Every timestamp is JST market
// data, so all datetime rendering is anchored to Asia/Tokyo and never depends on the
// viewer's browser timezone.

export const EMPTY = '—'

const JST = 'Asia/Tokyo'

// Persisted stamps carry an explicit +09:00 offset from the writer. The fallback
// appends it so an offset-less stamp is still read as JST wall-clock rather than the
// viewer's local time.
function toInstant(value: string): Date {
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(value)
  return new Date(hasZone ? value : `${value}+09:00`)
}

// Parts are assembled manually to keep the output locale-stable and testable.
function jstParts(value: string) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: JST,
    hourCycle: 'h23',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).formatToParts(toInstant(value))
  const get = (type: string) => parts.find((part) => part.type === type)?.value ?? ''
  return {
    year: get('year'),
    month: get('month'),
    day: get('day'),
    hour: get('hour'),
    minute: get('minute'),
  }
}

/** Full JST timestamp as `YYYY-MM-DD HH:mm` (publish / run / execution stamps). */
export function formatJstDateTime(value: string): string {
  const parts = jstParts(value)
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`
}

/** Compact JST timestamp as `MM/DD HH:mm` for inline as-of badges. */
export function formatJstStamp(value: string): string {
  const parts = jstParts(value)
  return `${parts.month}/${parts.day} ${parts.hour}:${parts.minute}`
}

const dateFormatter = new Intl.DateTimeFormat('ja-JP', {
  timeZone: JST,
  year: 'numeric',
  month: 'short',
  day: 'numeric',
  weekday: 'short',
})

/** JST calendar date with weekday, e.g. `2026年7月19日(日)`. */
export function formatJstDate(value: string | null): string {
  if (value === null) return '日時なし'
  return dateFormatter.format(new Date(`${value}T00:00:00+09:00`))
}

const yenFormatter = new Intl.NumberFormat('ja-JP', {
  style: 'currency',
  currency: 'JPY',
  maximumFractionDigits: 0,
})

/** Whole-yen currency, e.g. `￥1,234,567`. */
export function formatYen(value: number): string {
  return yenFormatter.format(value)
}

/** Grouped number with a bounded fraction, e.g. `1,234.57`. */
export function formatNumber(value: number, digits = 0): string {
  return value.toLocaleString('ja-JP', { maximumFractionDigits: digits })
}

interface PctOptions {
  readonly fraction?: boolean
  readonly digits?: number
  readonly sign?: boolean
}

/** Percentage text, e.g. `+12.3%`. `fraction` scales a 0–1 ratio to percent. */
export function formatPct(value: number, { fraction = false, digits = 1, sign = false }: PctOptions = {}): string {
  const percentage = fraction ? value * 100 : value
  const prefix = sign && percentage > 0 ? '+' : ''
  return `${prefix}${percentage.toFixed(digits)}%`
}
