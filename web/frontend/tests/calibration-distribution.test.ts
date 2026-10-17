import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { ErLevelCalibrationContextView } from '../src/api/types'
import { CalibrationDistribution } from '../src/pages/stocks/StockAnalysisSection'

const context: ErLevelCalibrationContextView = {
  interpretation: 'observed_distribution_only', required_asofs: ['2020-01-31', '2020-02-28'],
  evidence_complete: false, evidence_blocking_reasons: ['entry_price_gap'],
  generated_at: '2026-10-01T00:00:00+09:00', valid_through: '2026-11-15',
  reference_horizon: '3y', screening_rules_hash: 'synthetic', er_model_version: 'expected-return-v1',
  primary_realized_basis: 'fy_actual_dividend_total_return', secondary_realized_basis: 'price_return_only',
  trap_basis: 'cohort_population_cumulative_return_excess_lte_minus_0_20',
  horizons: [{ horizon: '3y', as_of_start: '2020-01-31', as_of_end: '2020-02-28', cohort_count: 2,
    bands: [{ band_id: 'q5', quintile: 5, lower_er_annual: .04, upper_er_annual: null,
      median_predicted_er_annual: .06, cohort_count: 2, median_n: 1,
      bases: [
        { basis: 'fy_actual_dividend_total_return', coverage: { declared_n: 2, observed_n: 0, missing_n: 2, missing_reason_counts: { unresolved_missing_dividend: 2 } }, observed_cohort_count: 0, median_n: 0, ticker_equal: null, cohort_equal: null },
        { basis: 'price_return_only', coverage: { declared_n: 2, observed_n: 2, missing_n: 0, missing_reason_counts: {} }, observed_cohort_count: 2, median_n: 1, ticker_equal: { n: 2, median: .01, q25: .01, q10: .01, trap_rate: 0 }, cohort_equal: { n: 1, median: .01, q25: .01, q10: .01, trap_rate: 0 } },
      ],
    }],
  }],
}

describe('記述較正分布', () => {
  it('formal evidenceの不成立で観測済み価格と宣言月を隠さない', () => {
    const html = renderToStaticMarkup(createElement(CalibrationDistribution, { context }))
    expect(html).toContain('宣言2月')
    expect(html).toContain('E[r]較正の証拠充足：未成立')
    expect(html).toContain('価格のみ')
    expect(html).toContain('1% / 1%')
    expect(html).toContain('2 / 2')
    expect(html).toContain('unresolved_missing_dividend: 2')
    expect(html).toContain('未評価')
    expect(html).toContain('全母集団の期待収益・個別予測・購入条件ではありません')
    expect(html).toContain('差は配当効果を表しません')
  })
})


it('固定帯の閾値は画面定数ではなく保存された境界を表示する', () => {
  const changed = structuredClone(context)
  const band = changed.horizons[0].bands[0]
  band.quintile = null
  band.band_id = 'fixed-threshold'
  band.lower_er_annual = .12
  const html = renderToStaticMarkup(createElement(CalibrationDistribution, { context: changed }))
  expect(html).toContain('E[r] ≥ 12%')
  expect(html).not.toContain('8.5%')
  band.lower_er_annual = null
  const missing = renderToStaticMarkup(createElement(CalibrationDistribution, { context: changed }))
  expect(missing).toContain('E[r]の閾値未記録')
  expect(missing).not.toContain('8.5%')
})
