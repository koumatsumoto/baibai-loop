import { describe, expect, it } from 'vitest'

import { pnlTone, totalUnrealizedPnl } from '../src/lib/portfolio'

describe('totalUnrealizedPnl', () => {
  it('calculates the displayed percentage against the full cost basis', () => {
    expect(totalUnrealizedPnl(395_000, 400_000)).toEqual({ yen: -5_000, pct: -1.25 })
  })

  it('keeps the total unknown when a held position cannot be valued', () => {
    expect(totalUnrealizedPnl(null, 200_000)).toEqual({ yen: null, pct: null })
    expect(totalUnrealizedPnl(100_000, null)).toEqual({ yen: null, pct: null })
  })

  it('does not divide by an empty cost basis', () => {
    expect(totalUnrealizedPnl(0, 0)).toEqual({ yen: 0, pct: null })
  })
})

describe('pnlTone', () => {
  // Monetary gains/losses use a different palette from other directional metrics.
  it.each([
    [12_000, 'text-profit'],
    [-12_000, 'text-loss'],
    [0, 'text-muted-foreground'],
    [null, 'text-muted-foreground'],
  ])('paints %s as %s', (value, expected) => {
    expect(pnlTone(value)).toBe(expected)
  })
})
