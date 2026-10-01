import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, it, vi } from 'vitest'

import type { HoldingView, ReviewSetView } from '../src/api/types'
import { TooltipProvider } from '../src/components/ui/tooltip'
import { HoldingsTable } from '../src/pages/DashboardPage'
import { ReviewSetSection } from '../src/pages/stocks/ReviewSetSection'

const exportsSeen = vi.hoisted(() => [] as { tickers: readonly string[]; filename: string }[])
vi.mock('../src/components/TradingViewWatchlistExportButton', () => ({
  TradingViewWatchlistExportButton: (props: { tickers: readonly string[]; filename: string }) => {
    exportsSeen.push(props)
    return null
  },
}))

describe('watchlist export sources', () => {
  it('passes every displayed holding including those without a market value', () => {
    exportsSeen.length = 0
    const holdings = ['7203', '130A'].map((ticker) => ({
      ticker, company_name: null, sector: '', quantity: 100, deployed_cost_yen: 1000,
      market_price_yen: null, market_price_as_of: null, market_value_yen: null,
      unrealized_pnl_yen: null, unrealized_pnl_pct: null, pmax_raw_yen: null,
      pmax_gap_pct: null, latest_thesis_id: null, disposition: null, next_earnings_date: null,
    }) satisfies HoldingView)
    renderToStaticMarkup(createElement(TooltipProvider, null,
      createElement(MemoryRouter, null, createElement(HoldingsTable, { holdings, warnings: [], ledgerAsOf: '2026-09-30T23:15:00+09:00' })),
    ))
    expect(exportsSeen).toMatchObject([{ tickers: ['7203', '130A'], filename: 'holdings-2026-09-30-tradingview.txt' }])
  })

  it('passes only the Review Set entries in their existing order', () => {
    exportsSeen.length = 0
    const analysis = {
      identity_liquidity: { market_cap_oku: null, avg_turnover_oku: null, listing_span_days: null, jpx_flags: null },
      valuation: { per_forward: null, per_trailing: null, pbr: null, ev_ebitda: null, p_s: null, pcfr: null },
      current_earnings: { fcf_yield: null, ocf_yield: null, forecast_special_gain_flag: null, forecast_full_year_loss_flag: null },
      normalized_earnings: { normalized_per_3fy: null, normalized_per_3fy_sector_gap: null },
      asset_value: { asset_backed_ratio: null, net_cash_to_market_cap: null, investment_securities: null, equity_ratio: null },
      reinvestment: null,
      expected_return: {
        er_annual: null, er_reversion_annual: null, er_carry_annual: null,
        fv_sector_median_yen: null, fv_self_range_yen: null, er_origin: null,
        er_model_version: null, er_unit: null, er_assumptions: null,
      },
      data_quality: { bs_carry_forward_fields: null, bs_carry_forward_lag_days: null, edinet_failure_reasons: null, stale_fin_flag: null },
      context: {
        next_earnings_status: null, next_earnings_estimated_date: null, margin_short_to_adv: null,
        tse_capital_policy_status: null, large_holding_filing_within_lookback: null,
        tender_offer_filing_within_lookback: null,
      },
    }
    const reviewSet = {
      created_at: '2026-09-30T18:00:00+09:00',
      entries: ['130A', '7203'].map((ticker) => ({
        ticker, name: ticker, sector_33: null, nominations: [], analysis,
      })),
    } as unknown as ReviewSetView
    renderToStaticMarkup(createElement(MemoryRouter, null,
      createElement(ReviewSetSection, { reviewSet, runAsOf: '2026-09-30' }),
    ))
    expect(exportsSeen).toMatchObject([{ tickers: ['130A', '7203'], filename: 'review-set-2026-09-30-tradingview.txt' }])
  })
})
