import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { SecurityAnalysisRowView } from '../src/api/types'
import { ScreeningMetrics } from '../src/pages/SecurityDetailPage'

const row = {
  er_annual: 0.12, er_reversion_annual: 0.04, er_carry_annual: 0.08,
  er_model_version: 'synthetic-model', er_unit: 'ratio_per_year', er_anchor_metrics: 'PBR+PER',
  er_assumptions: 'fixed realization and cap',
  dividend_yield: null, dividend_basis: 'unavailable', net_share_change_yoy: null,
  carry_missing_assumption: 'Missing carry inputs contribute zero; missing is not observed zero.',
  market_cap_oku: null, fair_value_anchor_yen: null, fair_value_gap_pct: null,
  margin_short_to_adv: null, data_quality_flags: [],
} as SecurityAnalysisRowView

describe('候補外も読める機械推計の説明', () => {
  it('保存モデル・単位・anchor・前提を年率と成分へ併記する', () => {
    const html = renderToStaticMarkup(createElement(ScreeningMetrics, { row }))
    for (const text of ['%/年', 'reversion / carry', '12.0%', '4.0%', '8.0%', 'synthetic-model', 'ratio_per_year', 'PBR+PER', 'fixed realization and cap']) expect(html).toContain(text)
    expect(html.match(/機械E\[r\]のモデル・前提/g)).toHaveLength(1)
    expect(html.match(/欠測の成分は計算上0寄与/g)).toHaveLength(1)
    expect(html).toContain('無配・株数不変の確認ではありません')
  })
  it('欠測の説明を補完しない', () => {
    const html = renderToStaticMarkup(createElement(ScreeningMetrics, { row: { ...row, er_model_version: null, er_unit: null, er_anchor_metrics: null, er_assumptions: null, carry_missing_assumption: null } }))
    expect(html).toContain('モデル 未確認')
    expect(html).toContain('保存単位 未確認')
    expect(html).toContain('保存された前提はありません')
    expect(html).not.toContain('fixed realization and cap')
    expect(html).not.toContain('欠測の成分は計算上0寄与')
  })
  it('欠測の成分と観測済みゼロを同じcarry値でも区別する', () => {
    const missing = renderToStaticMarkup(createElement(ScreeningMetrics, { row }))
    const observedZero = renderToStaticMarkup(createElement(ScreeningMetrics, { row: { ...row, dividend_yield: 0, dividend_basis: 'actual_reported', net_share_change_yoy: 0 } }))
    expect(missing.match(/未観測・未記録/g)).toHaveLength(2)
    expect(missing).toContain('basis: unavailable')
    expect(observedZero).not.toContain('未観測・未記録')
    expect(observedZero.match(/>0\.0%</g)).toHaveLength(2)
    expect(observedZero).toContain('basis: actual_reported')
    expect(observedZero).toContain('fixed realization and cap')
  })
})
