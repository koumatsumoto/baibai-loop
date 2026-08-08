import { describe, expect, it } from 'vitest'

import type { HoldingView } from '../src/api/types'
import { pnlTone, totalUnrealizedPnl } from '../src/lib/portfolio'

function holding(overrides: Partial<HoldingView>): HoldingView {
  return {
    ticker: '0000',
    company_name: null,
    sector: 'Test',
    quantity: 100,
    deployed_cost_yen: 100_000,
    market_price_yen: '1100',
    market_price_as_of: '2026-07-23T15:30:00+09:00',
    market_value_yen: 110_000,
    unrealized_pnl_yen: 10_000,
    unrealized_pnl_pct: 10,
    fair_value_yen: null,
    fv_gap_pct: null,
    latest_thesis_id: null,
    recommendation: null,
    next_earnings_date: null,
    ...overrides,
  }
}

describe('totalUnrealizedPnl', () => {
  it('sums yen and weights the percentage by deployed cost', () => {
    expect(totalUnrealizedPnl([
      holding({ deployed_cost_yen: 100_000, unrealized_pnl_yen: 10_000 }),
      holding({ deployed_cost_yen: 300_000, unrealized_pnl_yen: -15_000 }),
    ])).toEqual({ yen: -5_000, pct: -1.25 })
  })

  it('does not divide by an empty cost basis', () => {
    expect(totalUnrealizedPnl([
      holding({ deployed_cost_yen: 0, unrealized_pnl_yen: 0 }),
    ])).toEqual({ yen: 0, pct: null })
  })
})

describe('pnlTone', () => {
  // Money outcomes wear gold and blue so they never read as the green/red every other
  // number uses for direction. Nothing else in the suite checks which token gets picked.
  it.each([
    [12_000, 'text-profit'],
    [-12_000, 'text-loss'],
    [0, 'text-muted-foreground'],
    [null, 'text-muted-foreground'],
  ])('paints %s as %s', (value, expected) => {
    expect(pnlTone(value)).toBe(expected)
  })
})
