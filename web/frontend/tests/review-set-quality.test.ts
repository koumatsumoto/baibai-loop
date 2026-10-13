import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'

import type { ReviewSetExpectedReturnView, ReviewSetReinvestmentView, ReviewSetView } from '../src/api/types'
import { ReviewSetSection } from '../src/pages/stocks/ReviewSetSection'

function renderQuality(
  ev: 'exact' | 'approximated' | 'unavailable' | null | undefined,
  fcf: 'exact' | 'approximated' | 'unavailable' | null | undefined,
  carry: Partial<ReviewSetExpectedReturnView> = {},
  reinvestment: ReviewSetReinvestmentView | null = null,
  averagePer: number | null = null,
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
        normalized_earnings: { normalized_per_3fy: averagePer, normalized_per_3fy_sector_gap: null },
        asset_value: { asset_backed_ratio: null, net_cash_to_market_cap: null, investment_securities: null, equity_ratio: null },
        reinvestment,
        expected_return: {
          er_annual: null, er_reversion_annual: null, er_carry_annual: null,
          fv_sector_median_yen: null, fv_self_range_yen: null, er_origin: null,
          er_model_version: null, er_unit: null, er_assumptions: null,
          dividend_yield: null, dividend_basis: null, net_share_change_yoy: null,
          carry_missing_assumption: null, ...carry,
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
  it('describes average EPS and the limited CF role without changing frozen numbers', () => {
    const html = renderQuality('exact', 'exact', {}, null, 11.25)
    expect(html).toContain('3FY平均EPSベースPER')
    expect(html).toMatch(/3FY平均EPSベースPER<\/dt><dd[^>]*>11.25<\/dd>/)
    expect(html).toContain('3FY平均EPSは一時損益・循環を調整した代表利益ではありません。')
    expect(html).toContain('利益倍率の探索でCFは同順位時の補助です。')
    expect(html).not.toContain('平常PER')
    expect(html).not.toContain('平常収益力')
  })

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

describe('partial Reinvestment observations', () => {
  it.each([
    [-0.01, '-1%'],
    [0, '0%'],
    [null, '—'],
  ] as const)('distinguishes known growth %s from missing growth', (salesYoY, expected) => {
    const html = renderQuality(null, null, {}, {
      p_s_sector_gap: -0.4, operating_return_on_capital_proxy: 0.1,
      sales_yoy: salesYoY, operating_margin: null, fcf_yield: 0.08,
    })
    expect(html).toMatch(new RegExp(`sales YoY</dt><dd[^>]*>${expected}</dd>`))
    expect(html).toMatch(/operating margin<\/dt><dd[^>]*>—<\/dd>/)
    expect(html).toMatch(/P\/S sector gap<\/dt><dd[^>]*>-0.4<\/dd>/)
    expect(html).not.toContain('not applicable')
  })
})

describe('machine carry inputs', () => {
  it('distinguishes missing inputs from observed zero with the same carry', () => {
    const missing = renderQuality(null, null, {
      er_carry_annual: 0, dividend_basis: 'unavailable', carry_missing_assumption: 'synthetic assumption',
    })
    const zero = renderQuality(null, null, {
      er_carry_annual: 0, dividend_yield: 0, net_share_change_yoy: 0,
      dividend_basis: 'actual_reported', carry_missing_assumption: 'synthetic assumption',
    })
    expect(missing).toContain('未観測・未記録')
    expect(missing).toContain('unavailable')
    expect(zero).not.toContain('未観測・未記録')
    expect(zero).toContain('actual_reported')
    expect(zero).toContain('0%')
    expect(missing).toContain('欠測の成分は計算上0寄与。無配・株数不変の確認ではありません。')
    expect(zero).toContain('欠測の成分は計算上0寄与。無配・株数不変の確認ではありません。')
  })

  it('shows that an old frozen snapshot has no carry metadata', () => {
    expect(renderQuality(null, null)).toContain('この保存snapshotには仮定の記録がありません。')
  })
})
