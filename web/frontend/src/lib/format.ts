// Saved timestamps use their own offsets; display is anchored to Asia/Tokyo.

export const EMPTY = '—'

const JST = 'Asia/Tokyo'

const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/

function isDateOnly(value: string): boolean {
  return DATE_ONLY.test(value)
}

// Date-only values use midnight solely for formatting; offset-less times use JST.
function toInstant(value: string): Date {
  if (isDateOnly(value)) return new Date(`${value}T00:00:00+09:00`)
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(value)
  return new Date(hasZone ? value : `${value}+09:00`)
}

const jstClock = new Intl.DateTimeFormat('en-US', {
  timeZone: JST,
  hourCycle: 'h23',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
})

// Parts are assembled manually to keep the output locale-stable and testable.
export function jstDateTimeParts(instant: Date) {
  const parts = jstClock.formatToParts(instant)
  const get = (type: string) => parts.find((part) => part.type === type)?.value ?? ''
  return {
    year: get('year'),
    month: get('month'),
    day: get('day'),
    hour: get('hour'),
    minute: get('minute'),
  }
}

/** JST timestamp as `YYYY-MM-DD HH:mm`, preserving date-only input as `YYYY-MM-DD`. */
export function formatJstDateTime(value: string): string {
  const parts = jstDateTimeParts(toInstant(value))
  const day = `${parts.year}-${parts.month}-${parts.day}`
  return isDateOnly(value) ? day : `${day} ${parts.hour}:${parts.minute}`
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
  readonly digits?: number
  readonly sign?: boolean
}

/** Format a value already in percent, e.g. 12.3 as `12.3%`. */
export function formatPct(value: number, { digits = 1, sign = false }: PctOptions = {}): string {
  const prefix = sign && value > 0 ? '+' : ''
  return `${prefix}${value.toFixed(digits)}%`
}
