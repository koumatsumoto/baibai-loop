// Single source for how the UI renders values. Saved timestamps may use different
// offsets; datetime display is anchored to Asia/Tokyo rather than the browser timezone.

export const EMPTY = '—'

const JST = 'Asia/Tokyo'

const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/

function isDateOnly(value: string): boolean {
  return DATE_ONLY.test(value)
}

// Persisted stamps retain their explicit offset, including UTC. A date carries no
// time at all; midnight JST is only its formatting anchor. An offset-less stamp
// is read as JST wall-clock rather than the viewer's local time. Appending an offset
// to a bare date would build
// `2026-07-29+09:00`, which is not a parseable ISO string.
function toInstant(value: string): Date {
  if (isDateOnly(value)) return new Date(`${value}T00:00:00+09:00`)
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

/** JST calendar date as `YYYY-MM-DD`, independent of the viewer's timezone. */
export function jstCalendarDate(value: string): string {
  const parts = jstParts(value)
  return `${parts.year}-${parts.month}-${parts.day}`
}

/** Days from one JST calendar date to another, without browser timezone arithmetic. */
export function daysUntilJstDate(value: string, today: string): number {
  return (Date.parse(`${jstCalendarDate(value)}T00:00:00+09:00`) - Date.parse(`${today}T00:00:00+09:00`)) / 86_400_000
}

/** JST timestamp as `YYYY-MM-DD HH:mm`, preserving date-only input as `YYYY-MM-DD`. */
export function formatJstDateTime(value: string): string {
  const parts = jstParts(value)
  const day = `${parts.year}-${parts.month}-${parts.day}`
  return isDateOnly(value) ? day : `${day} ${parts.hour}:${parts.minute}`
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
  return dateFormatter.format(toInstant(value))
}

const shortDateFormatter = new Intl.DateTimeFormat('ja-JP', {
  timeZone: JST,
  month: 'numeric',
  day: 'numeric',
  weekday: 'short',
})

/**
 * Compact JST calendar date, e.g. `7/19 (日)`. For lists whose own window already
 * fixes the year — dropping it there costs nothing and keeps the column narrow.
 */
export function formatJstDateShort(value: string): string {
  const parts = shortDateFormatter.formatToParts(toInstant(value))
  const get = (type: string) => parts.find((part) => part.type === type)?.value ?? ''
  return `${get('month')}/${get('day')} (${get('weekday')})`
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

const unitPriceFormatter = new Intl.NumberFormat('ja-JP', {
  style: 'currency',
  currency: 'JPY',
  maximumFractionDigits: 4,
})

/** Per-share yen, retaining the ledger's supported four decimal places. */
export function formatUnitPriceYen(value: number): string {
  return unitPriceFormatter.format(value)
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
