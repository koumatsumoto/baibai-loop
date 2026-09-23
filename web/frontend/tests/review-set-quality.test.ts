import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'

import type { ReviewSetView } from '../src/api/types'
import { ReviewSetSection } from '../src/pages/stocks/ReviewSetSection'

function renderQuality(
  ev: 'exact' | 'approximated' | 'unavailable' | null | undefined,
  fcf: 'exact' | 'approximated' | 'unavailable' | null | undefined,
): string {
  const dataQuality = {
    bs_carry_forward_fields: null,
    bs_carry_forward_lag_days: null,
    edinet_failure_reasons: null,
    stale_fin_flag: null,
    ...(ev === undefined ? {} : { ttm_quality_ev_ebitda: ev }),
    ...(fcf === undefined ? {} : { ttm_quality_fcf: fcf }),
  }
  const reviewSet = {
    review_set_id: 'review-1',
    run_revision_id: 'run-1',
    created_at: '2026-09-19T18:00:00+09:00',
    entries: [{
      ticker: '1301', name: 'sample', sector_33: '情報・通信業',
      nominations: [], er_origin: null, er_model_version: null,
      er_unit: null, er_assumptions: null,
      analysis: {
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
        data_quality: dataQuality,
        context: {
          next_earnings_status: null, next_earnings_estimated_date: null,
          margin_short_to_adv: null, tse_capital_policy_status: null,
          large_holding_filing_within_lookback: null, tender_offer_filing_within_lookback: null,
        },
      },
    }],
  } as ReviewSetView
  return renderToStaticMarkup(
    createElement(MemoryRouter, null, createElement(ReviewSetSection, { reviewSet, runAsOf: "2026-09-19" })),
  )
}

describe('Review Set TTM quality', () => {
  it.each([
    ['exact', 'exact', false, false],
    ['approximated', 'unavailable', true, true],
    [null, null, false, false],
    [undefined, undefined, false, false],
  ] as const)('shows only nonexact values: %s / %s', (ev, fcf, showEv, showFcf) => {
    const html = renderQuality(ev, fcf)
    expect(html.includes('EV/EBITDA TTM')).toBe(showEv)
    expect(html.includes('FCF TTM')).toBe(showFcf)
    if (showEv) expect(html).toContain('approximated')
    if (showFcf) expect(html).toContain('unavailable')
  })
})
