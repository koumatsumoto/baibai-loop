// Single source for how Baibai Loop renders values. Every timestamp is JST market
// data, so all datetime rendering is anchored to Asia/Tokyo and never depends on the
// viewer's browser timezone.

export const EMPTY = '—'

const JST = 'Asia/Tokyo'

const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/

function isDateOnly(value: string): boolean {
  return DATE_ONLY.test(value)
}

// Persisted stamps carry an explicit +09:00 offset from the writer. A date carries no
// time at all and means midnight JST; an offset-less stamp is read as JST wall-clock
// rather than the viewer's local time. Appending an offset to a bare date would build
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

/** Full JST timestamp as `YYYY-MM-DD HH:mm` (publish / run / execution stamps). */
export function formatJstDateTime(value: string): string {
  const parts = jstParts(value)
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`
}

/**
 * Compact JST timestamp as `MM/DD HH:mm` for inline as-of badges, or `MM/DD` when the
 * value carries no time of day. A reading computed for a date has no time, and printing
 * `00:00` for it would state a precision the value does not have.
 */
export function formatJstStamp(value: string): string {
  const parts = jstParts(value)
  if (isDateOnly(value)) return `${parts.month}/${parts.day}`
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

const dateKeyFormatter = new Intl.DateTimeFormat('sv-SE', {
  timeZone: JST,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
})

/**
 * True when the value is `days` or more JST calendar days behind today. Freshness is a
 * question about market days, so it compares dates rather than elapsed hours — and it
 * goes through the shared parser, so an offset-less stamp is not read in the viewer's
 * timezone.
 */
export function isOlderThanDays(value: string, days: number): boolean {
  const today = dateKeyFormatter.format(new Date())
  const asOf = dateKeyFormatter.format(toInstant(value))
  return Date.parse(`${today}T00:00:00Z`) - Date.parse(`${asOf}T00:00:00Z`) >= days * 86_400_000
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
