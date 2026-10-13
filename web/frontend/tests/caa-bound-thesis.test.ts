import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'

import type { AllocationAlternativeView } from '../src/api/types'
import { AllocationThesisDetails } from '../src/pages/CapitalAllocationAssessmentPage'

const item: AllocationAlternativeView = {
  ticker: '1234', disposition: 'decline', rationale: '当時の比較',
  thesis_id: 'thesis-1234-original', thesis_review_id: 'review-original',
  case_status: 'intact', base_annualized_return_pct: 23.4, pmax_raw_yen: 1101.7857142857142,
  valuation_as_of: '2026-09-07',
  thesis_projection: { status: 'resolved', as_of: '2026-09-07',
    investment_case: { explanation: '当時の企業価値の根拠', status: 'intact' },
    projections: { base: { terminal_value_per_share_yen: 1204, cash_distribution_per_share_yen: 30 } },
    raw: { input_snapshot: { sources: [{ ref: 'https://example.invalid/original-source' }] } },
  },
  thesis_review: { review_id: 'review-original', reviewer_identity: '当時の独立reviewer', strongest_countercase: '当時の反対仮説' },
}
function render(value = item) {
  return renderToStaticMarkup(createElement(MemoryRouter, null, createElement(AllocationThesisDetails, { item: value })))
}

describe('CAAが参照した企業評価', () => {
  it('固定Thesis/Reviewと根拠・出典・原評価を表示し、最新情報を別リンクにする', () => {
    const html = render()
    for (const text of ['caa-thesis-thesis-1234-original', 'CAA参照の企業評価', 'thesis-1234-original', 'review-original', '当時の企業価値の根拠', '当時の独立reviewer', '当時の反対仮説', 'original-source', '1,101.79円', '最新の銘柄情報・企業評価を見る']) expect(html).toContain(text)
    expect(html).toContain('href="/securities/1234"')
    expect(html).not.toContain('1,637.50')
  })
  it('保存された旧形式の本文を閲覧し、Review未記録を補完しない', () => {
    const html = render({ ...item, thesis_projection: { status: 'requires_reassessment', raw: { explanation: '旧本文の根拠' } }, thesis_review: null, thesis_review_id: null })
    expect(html).toContain('旧本文の根拠')
    expect(html).toContain('参照未記録')
    expect(html).toContain('参照したReview本文は記録されていません')
    expect(html).not.toContain('当時の独立reviewer')
    expect(render({ ...item, thesis_review: null })).toContain('参照したReview本文を取得できませんでした')
    expect(render({ ...item, thesis_projection: {} })).toContain('企業評価の本文を参照できません')
  })
})
