import { describe, expect, it } from 'vitest'

import {
  EMPTY,
  formatJstDate,
  formatJstDateShort,
  formatJstDateTime,
  formatJstStamp,
  formatNumber,
  formatPct,
  formatYen,
  isOlderThanDays,
} from '../src/lib/format'

describe('formatJstDateTime', () => {
  it('renders a +09:00 stamp as JST wall-clock YYYY-MM-DD HH:mm', () => {
    expect(formatJstDateTime('2026-07-19T12:00:00+09:00')).toBe('2026-07-19 12:00')
  })

  it('converts a UTC instant to JST (+9h)', () => {
    expect(formatJstDateTime('2026-07-19T03:00:00Z')).toBe('2026-07-19 12:00')
  })

  it('reads an offset-less stamp as JST rather than the viewer timezone', () => {
    expect(formatJstDateTime('2026-07-19T12:00:00')).toBe('2026-07-19 12:00')
  })

  // A bare date is JST midnight, not UTC midnight — reading it as UTC would land the
  // same calendar day at 09:00 and go unnoticed everywhere the time is not shown.
  it('reads a bare date as JST midnight', () => {
    expect(formatJstDateTime('2026-07-19')).toBe('2026-07-19 00:00')
  })

  it('keeps the same instant across a non-JST offset', () => {
    expect(formatJstDateTime('2026-07-18T23:00:00-04:00')).toBe('2026-07-19 12:00')
  })
})

describe('formatJstStamp', () => {
  it('renders a compact MM/DD HH:mm in JST', () => {
    expect(formatJstStamp('2026-07-19T03:00:00Z')).toBe('07/19 12:00')
  })

  // A value with no time of day has no 00:00 to state.
  it('drops the time for a calendar date', () => {
    expect(formatJstStamp('2026-07-19')).toBe('07/19')
  })

  it('reads a bare date as JST midnight rather than failing to parse', () => {
    expect(formatJstStamp('2026-01-01')).toBe('01/01')
  })
})

describe('formatJstDate', () => {
  it('renders the JST calendar date with weekday', () => {
    expect(formatJstDate('2026-07-19')).toBe('2026年7月19日(日)')
  })

  it('returns a placeholder for a missing date', () => {
    expect(formatJstDate(null)).toBe('日時なし')
  })
})

describe('formatJstDateShort', () => {
  it('drops the year and keeps the weekday', () => {
    expect(formatJstDateShort('2026-07-19')).toBe('7/19 (日)')
  })

  it('leaves the month and day unpadded', () => {
    expect(formatJstDateShort('2026-01-05')).toBe('1/5 (月)')
  })

  it('reads an instant in JST rather than the viewer timezone', () => {
    // 20:00 UTC on new year's eve is already the next year in Tokyo.
    expect(formatJstDateShort('2026-12-31T20:00:00Z')).toBe('1/1 (金)')
  })
})

describe('isOlderThanDays', () => {
  const today = new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Tokyo',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(new Date())
  const daysAgo = (days: number) =>
    new Date(Date.parse(`${today}T00:00:00Z`) - days * 86_400_000).toISOString().slice(0, 10)

  it('is false inside the window', () => {
    expect(isOlderThanDays(daysAgo(6), 7)).toBe(false)
  })

  it('is true on and past the boundary', () => {
    expect(isOlderThanDays(daysAgo(7), 7)).toBe(true)
    expect(isOlderThanDays(daysAgo(30), 7)).toBe(true)
  })

  // The bare date must not be read in the viewer's timezone, which would shift the
  // boundary by a day for anyone outside JST.
  it('accepts a stamp with no offset', () => {
    expect(isOlderThanDays(`${daysAgo(6)}T09:00:00`, 7)).toBe(false)
  })
})

describe('formatYen', () => {
  it('formats whole yen with the ¥ symbol and grouping', () => {
    expect(formatYen(1234567)).toBe('￥1,234,567')
  })

  it('rounds to whole yen', () => {
    expect(formatYen(1234.6)).toBe('￥1,235')
  })
})

describe('formatNumber', () => {
  it('groups thousands and rounds to the requested fraction digits', () => {
    expect(formatNumber(1234.567, 2)).toBe('1,234.57')
  })

  it('defaults to zero fraction digits', () => {
    expect(formatNumber(1234.9)).toBe('1,235')
  })
})

describe('formatPct', () => {
  it('formats a raw percentage to one decimal', () => {
    expect(formatPct(12.34)).toBe('12.3%')
  })

  it('scales a 0-1 fraction to percent', () => {
    expect(formatPct(0.1234, { fraction: true })).toBe('12.3%')
  })

  it('adds a leading + only for positive values when signed', () => {
    expect(formatPct(1.2, { sign: true })).toBe('+1.2%')
    expect(formatPct(-1.2, { sign: true })).toBe('-1.2%')
    expect(formatPct(0, { sign: true })).toBe('0.0%')
  })

  it('honours a custom digit count', () => {
    expect(formatPct(1.239, { digits: 2 })).toBe('1.24%')
  })
})

describe('EMPTY', () => {
  it('is the em dash placeholder', () => {
    expect(EMPTY).toBe('—')
  })
})
