import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'

import type { InvestmentProposalItemView, InvestmentProposalPreviewView } from '../src/api/types'
import { CurrentPreviewContent, ProposalAssessmentDetails, ProposalItemContent } from '../src/pages/InvestmentProposalPage'

const document = {
  input_snapshot: { as_of: '2026-10-14', sources: [{ ref: 'https://example.invalid/original-source' }] },
  findings: { business_understanding: '保存した収益構造', value_gap_explanation: '1000円以下で要求率を満たす', realization: { mechanism: '収益維持の確認', timing: 'uncertain', timing_reason: '時期は確約されない', delay_risk: '評価の長期化' }, hypotheses: [{ statement: 'ユーザーの仮説', result: 'supported', reason: '一次資料で検証' }], unresolved_questions: [] },
  investment_case: { explanation: '当時の投資仮説', status: 'intact', status_reason: '反証なし', invalidation_conditions: ['主要顧客喪失'] },
  conclusion: { strongest_countercase: '需要縮小' },
  valuation: { status: 'resolved', horizon_months: 12, required_annual_return_pct: '10', base: { terminal_value_per_share_yen: '1100', cash_distribution_per_share_yen: '0', calculation: '保存したBase計算' }, downside: { terminal_value_per_share_yen: '700', cash_distribution_per_share_yen: '0', calculation: '保存したDownside計算' } },
}
const item = {
  ticker: '1234', action: 'buy', rationale: '当時1100円、1000円の指値で待つ', timing: 'wait_limit',
  assessment_ref: { assessment_id: 'stock-assessment-original', core_sha256: 'a'.repeat(64), review_id: 'assessment-review-original' },
  proposed_limit_yen: null, review_conditions: ['仮説の反証時に再確認'], order_readiness: 'ready',
  terms: { side: 'buy', limit_price_yen: '1000', quantity: 300, notional_yen: '300000', target_session: '2026-10-15', expires_at: '2026-10-15T15:30:00+09:00', valuation_as_of: '2026-10-14', maximum_entry_price_yen: '1000', price_grid_rule_id: 'tse-common-conservative-20261014', conditions: ['仮説・株式単位・資金に重要変化が無いこと'], warnings: ['dry_powder_below_floor'] },
  terms_unavailable_reasons: [], blockers: [], resume_conditions: [], remaining_reward: null,
  assessment_projection: { raw: document, original_price_yen: 1100, original_quote_at: '2026-10-14T15:30:00+09:00', original_price_basis: 'last_close_unadjusted', projections: { base: { total_return_pct: 0, annualized_return_pct: 0 }, downside: { total_return_pct: -36.3636, annualized_return_pct: -36.3636 } } },
  assessment_review: { review_id: 'assessment-review-original', reviewer_identity: '保存した独立reviewer', review_scope: '保存根拠と今回の値を確認' },
} as InvestmentProposalItemView

function render(component: typeof ProposalItemContent | typeof ProposalAssessmentDetails, value = item) {
  return renderToStaticMarkup(createElement(MemoryRouter, null, createElement(component, { item: value })))
}

describe('保存した提案と評価', () => {
  it('exact評価/Reviewと根拠・所見・原価格を表示する', () => {
    const html = render(ProposalAssessmentDetails)
    for (const text of ['stock-assessment-original', 'assessment-review-original', '保存した収益構造', '当時の投資仮説', 'original-source', '保存した独立reviewer', 'ユーザーの仮説', '原評価の観測価格 1100']) expect(html).toContain(text)
    expect(html).toContain('href="/stocks/stock-assessments/stock-assessment-original"')
    expect(html).not.toContain('最新評価の根拠')
  })
  it('保存limit/quantity/expiry/warningsを示し、注文や約定と区別する', () => {
    const html = render(ProposalItemContent)
    for (const text of ['保存した提案条件', '提案条件確定', '1,000円', '300株', '300,000円', '2026-10-15', '指定の指値で待つ提案', '買付後の現金余力が警告水準を下回ります', '注文受理・約定の保証ではありません']) expect(html).toContain(text)
  })
  it('粗い共通呼値で実quoteより下がった提案価格の差を表示する', () => {
    const value = { ...item, timing: 'at_current_limit', terms: { ...item.terms!, limit_price_yen: '950', notional_yen: '285000' } }
    const html = renderToStaticMarkup(createElement(MemoryRouter, null, createElement(ProposalItemContent, { item: value, savedQuote: { price_yen: '950.5', price_as_of: '2026-10-14' } })))
    for (const text of ['提案時の実 quote 950.5円', '実 quote と提案指値の差 0.5円', '当時の現在価格を上限とする指値提案']) expect(html).toContain(text)
  })
  it('grid未確認でも買付判断と希望価格を残し、具体条件未完了を示す', () => {
    const html = render(ProposalItemContent, { ...item, proposed_limit_yen: '900', terms: null, order_readiness: 'needs_terms', terms_unavailable_reasons: [{ code: 'price_grid_rule_unavailable', message: '呼値制度の確認待ち' }] })
    for (const text of ['購入の提案', '価格・数量等の条件未確定', '希望価格の上限 900円', '呼値制度の確認待ち']) expect(html).toContain(text)
    expect(html).not.toContain('提案条件確定')
    expect(html).not.toContain('300株')
  })
  it('broken売却を価格未確定で保有にせず、経済判断と不足を別表示する', () => {
    const html = render(ProposalItemContent, { ...item, action: 'sell', timing: null, reason_code: 'economic_invalidation', case_reassessment_reason: '収益源の喪失を確認', terms: null, order_readiness: 'needs_terms', terms_unavailable_reasons: [{ code: 'quote_missing_or_stale', message: '現在quote欠測' }] })
    for (const text of ['売却の提案', '投資仮説の破綻', '収益源の喪失を確認', '現在quote欠測']) expect(html).toContain(text)
    expect(html).not.toContain('継続保有の提案')
  })
  it('未調査deferを経済的な見送りへ変換しない', () => {
    const html = render(ProposalItemContent, { ...item, action: 'defer', assessment_ref: null, terms: null, order_readiness: 'not_applicable', blockers: [{ code: 'research_unfinished', message: '一次資料の調査未完了' }], resume_conditions: ['一次資料を確認する'] })
    for (const text of ['判断保留', '一次資料の調査未完了', '一次資料を確認する', '完成評価への参照はありません']) expect(html).toContain(text)
    expect(html).not.toContain('見送り')
    expect(html).not.toContain('stock-assessment-original')
  })
  it('古いvaluationで仮説維持holdをした際に十分な現在収益を主張しない', () => {
    const html = render(ProposalItemContent, { ...item, action: 'hold', holding_basis: 'investment_case_intact', terms: null, case_reassessment_reason: '今回の反証確認で仮説維持', return_calculation_unavailable_reason: 'valuation_basis_requires_refresh', order_readiness: 'not_applicable' })
    for (const text of ['継続保有の提案', '投資仮説の維持を確認', '期間と今後分配を当日起点で再確認する必要']) expect(html).toContain(text)
    expect(html).not.toContain('現在からの見返りを確認')
    expect(html).not.toContain('保存した提案条件')
  })
  it('表示できない原文を最新評価で補わない', () => {
    expect(render(ProposalAssessmentDetails, { ...item, assessment_projection: {} })).toContain('参照評価の本文を表示できません')
  })
  it('現在quote再試算と保存limit再検証を別表示し、渡されたpreviewを変更しない', () => {
    const preview = { saved_proposal_id: 'investment-proposal-original', previewed_at: '2026-10-14T18:00:00+09:00', status: 'changed', current_inputs: { available_cash_yen: '200000' }, change_reasons: [{ code: 'inputs_changed', message: 'cashが変化しました' }], items: [{ ticker: '1234', saved_action: 'buy', current_quote_calculation: { calculation_price_yen: '1100', quantity: 0, current_price_projection: { base: { total_return_pct: 0 }, downside: { total_return_pct: -36.3636 } } }, saved_limit_revalidation: { quantity: 200 }, blockers: [] }], warnings: ['current-warning'], portfolio_exposure: null } as InvestmentProposalPreviewView
    const originalPreview = JSON.stringify(preview)
    const html = renderToStaticMarkup(createElement(CurrentPreviewContent, { preview }))
    for (const text of ['現在 quote での再試算', '試算価格 1100円', '保存 limit の再検証', '現在資金等での試算数量 200株', 'cashが変化しました', '保存した価格・数量・期限を更新していません']) expect(html).toContain(text)
    expect(JSON.stringify(preview)).toBe(originalPreview)
    expect(render(ProposalItemContent)).toContain('300株')
  })
  it('再計算不能の保有判断では理由を示し、未確認の収益率を計算結果に見せない', () => {
    const preview = { saved_proposal_id: 'original', previewed_at: '2026-10-14T18:00:00+09:00', status: 'changed', current_inputs: {}, change_reasons: [], items: [{ ticker: '1234', saved_action: 'hold', current_quote_calculation: { action: 'hold', current_price_projection: null, return_calculation_unavailable_reason: 'valuation_basis_requires_refresh' }, saved_limit_revalidation: null, blockers: [] }], warnings: [], portfolio_exposure: null } as InvestmentProposalPreviewView
    const html = renderToStaticMarkup(createElement(CurrentPreviewContent, { preview }))
    expect(html).toContain('再試算できません。期間と今後分配を当日起点で再確認する必要があります')
    expect(html).not.toContain('試算価格')
    expect(html).not.toContain('Base')
  })
  it('売却条件の再検証数量を現在資金からの再計算数量と説明しない', () => {
    const preview = { saved_proposal_id: 'original', previewed_at: '2026-10-14T18:00:00+09:00', status: 'blocked', current_inputs: {}, change_reasons: [], items: [{ ticker: '1234', saved_action: 'sell', current_quote_calculation: null, saved_limit_revalidation: { limit_price_yen: '1000', quantity: 300 }, blockers: [{ code: 'quantity_basis_unconfirmed', message: '保存売却数量は現在の全保有数量へ一致しません。' }] }], warnings: [], portfolio_exposure: null } as InvestmentProposalPreviewView
    const html = renderToStaticMarkup(createElement(CurrentPreviewContent, { preview }))
    expect(html).toContain('保存した売却数量 300株')
    expect(html).toContain('保存売却数量は現在の全保有数量へ一致しません')
    expect(html).not.toContain('現在資金等での試算数量')
  })

})
