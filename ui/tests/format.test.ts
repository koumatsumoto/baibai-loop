import { describe, expect, it } from 'vitest'

import {
  EMPTY,
  formatJstDate,
  formatJstDateTime,
  formatJstStamp,
  formatNumber,
  formatPct,
  formatYen,
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

  it('keeps the same instant across a non-JST offset', () => {
    expect(formatJstDateTime('2026-07-18T23:00:00-04:00')).toBe('2026-07-19 12:00')
  })
})

describe('formatJstStamp', () => {
  it('renders a compact MM/DD HH:mm in JST', () => {
    expect(formatJstStamp('2026-07-19T03:00:00Z')).toBe('07/19 12:00')
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
