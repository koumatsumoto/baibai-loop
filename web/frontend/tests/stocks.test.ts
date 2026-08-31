import { describe, expect, it } from 'vitest'

import type { SecurityAnalysisRowView } from '../src/api/types'
import {
  filterSecurityRows,
  parseOptionalNumber,
  projectSecurityRows,
  sortSecurityRows,
  type SecurityFilters,
} from '../src/lib/stocks'

function row(
  ticker: string,
  overrides: Partial<SecurityAnalysisRowView> = {},
): SecurityAnalysisRowView {
  return {
    ticker,
    name: `会社${ticker}`,
    sector_33: '情報・通信業',
    market_cap_oku: 100,
    avg_turnover_oku: 2,
    per_trailing: 10,
    normalized_per_3fy: 11,
    per_forward: 9,
    pbr: 1,
    ev_ebitda: 6,
    p_s: 1.2,
    pcfr: 8,
    dividend_yield: 0.03,
    dividend_basis: 'annual',
    dividend_split_factor: 1,
    er_annual: 0.1,
    er_reversion_annual: 0.06,
    er_carry_annual: 0.04,
    net_cash_to_market_cap: 0.2,
    fcf_yield: 0.07,
    ocf_yield: 0.08,
    equity_ratio: 0.55,
    sales_yoy: 0.08,
    operating_profit_yoy: 0.12,
    sector_relative_strength_percentile: 0.7,
    price_change_20d: 0.02,
    gap_from_52w_low: 0.25,
    next_earnings_date: '2026-09-10',
    margin_week_end: '2026-08-28',
    margin_long_to_adv: 2,
    margin_short_to_adv: 0.4,
    margin_long_share: 0.1,
    margin_long_delta_26w: 0.01,
    margin_std_long_share: 0.08,
    data_quality_flags: [],
    portfolio_state: 'none',
    has_research: false,
    fair_value_anchor_yen: 1200,
    fair_value_gap_pct: 20,
    er_level_quintile: null,
    er_meets_8_5pct_band: false,
    ...overrides,
  }
}

const noFilters: SecurityFilters = {
  query: '',
  sector: 'all',
  perMax: null,
  pbrMax: null,
  dividendMinPct: null,
  heldOnly: false,
  researchOnly: false,
}

describe('Security Analysis filters', () => {
  const rows = [
    row('1301', { name: 'Alpha Foods', sector_33: '水産・農林業' }),
    row('2331', {
      name: 'ＡＬＰＨＡ通信',
      per_trailing: 18,
      pbr: 2,
      dividend_yield: 0.01,
      portfolio_state: 'held',
      has_research: true,
    }),
  ]

  it('uses ticker prefix or company-name substring matching', () => {
    expect(filterSecurityRows(rows, { ...noFilters, query: '13' }).map((item) => item.ticker)).toEqual(['1301'])
    expect(filterSecurityRows(rows, { ...noFilters, query: 'alpha' }).map((item) => item.ticker)).toEqual(['1301', '2331'])
    expect(filterSecurityRows(rows, { ...noFilters, query: 'alpha通信' }).map((item) => item.ticker)).toEqual(['2331'])
    expect(filterSecurityRows(rows, { ...noFilters, query: '01' })).toEqual([])
  })

  it('combines sector, numeric, portfolio and research filters with AND', () => {
    expect(filterSecurityRows(rows, {
      ...noFilters,
      sector: '情報・通信業',
      perMax: 20,
      pbrMax: 2,
      dividendMinPct: 1,
      heldOnly: true,
      researchOnly: true,
    }).map((item) => item.ticker)).toEqual(['2331'])
  })

  it('treats a blank numeric filter as absent', () => {
    expect(parseOptionalNumber('')).toBeNull()
    expect(parseOptionalNumber(' 12.5 ')).toBe(12.5)
    expect(parseOptionalNumber('not-a-number')).toBeNull()
  })
})

describe('Security Analysis sorting and pagination', () => {
  it('defaults cleanly to ticker ascending and keeps nulls last in both directions', () => {
    const rows = [row('3', { pbr: null }), row('2', { pbr: 2 }), row('1', { pbr: 1 })]
    expect(sortSecurityRows(rows, { key: 'ticker', direction: 'asc' }).map((item) => item.ticker)).toEqual(['1', '2', '3'])
    expect(sortSecurityRows(rows, { key: 'pbr', direction: 'asc' }).map((item) => item.ticker)).toEqual(['1', '2', '3'])
    expect(sortSecurityRows(rows, { key: 'pbr', direction: 'desc' }).map((item) => item.ticker)).toEqual(['2', '1', '3'])
  })

  it('returns no more than 100 rows and clamps a stale page after filtering', () => {
    const rows = Array.from({ length: 205 }, (_, index) => row(String(index).padStart(4, '0')))
    const second = projectSecurityRows(rows, noFilters, { key: 'ticker', direction: 'asc' }, 2)
    expect(second.rows).toHaveLength(100)
    expect(second.rows[0].ticker).toBe('0100')

    const filtered = projectSecurityRows(
      rows,
      { ...noFilters, query: '0204' },
      { key: 'ticker', direction: 'asc' },
      3,
    )
    expect(filtered.page).toBe(1)
    expect(filtered.rows.map((item) => item.ticker)).toEqual(['0204'])
  })
})
