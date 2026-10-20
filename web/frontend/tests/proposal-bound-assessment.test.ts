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
const item: InvestmentProposalItemView = {
  position: null, holding_basis: null, case_reassessment_reason: null,
  return_calculation_unavailable_reason: null, reason_code: null,
  ticker: '1234', action: 'buy', rationale: '当時1100円、1000円の指値で待つ', timing: 'wait_limit',
  assessment_ref: { assessment_id: 'stock-assessment-original', core_sha256: 'a'.repeat(64), review_id: 'assessment-review-original' },
  proposed_limit_yen: null, review_conditions: ['仮説の反証時に再確認'], order_readiness: 'ready',
  terms: { side: 'buy', limit_price_yen: '1000', quantity: 300, notional_yen: '300000', target_session: '2026-10-15', valuation_as_of: '2026-10-14', maximum_entry_price_yen: '1000', price_grid_rule_id: 'tse-common-conservative-20261014', conditions: ['仮説・株式単位・資金に重要変化が無いこと'], warnings: ['dry_powder_below_floor'] },
  terms_unavailable_reasons: [], blockers: [], resume_conditions: [], remaining_reward: null,
  assessment_projection: { raw: document, original_price_yen: 1100, original_quote_at: '2026-10-14T15:30:00+09:00', original_price_basis: 'last_close_unadjusted', projections: { base: { total_return_pct: 0, annualized_return_pct: 0 }, downside: { total_return_pct: -36.3636, annualized_return_pct: -36.3636 } } },
  assessment_review: { review_id: 'assessment-review-original', reviewer_identity: '保存した独立reviewer', review_scope: '保存根拠と今回の値を確認' },
}

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
  it('合成例の個社経路・比較不能な外部相違・反証を保存sourceとReviewへ辿れる', () => {
    const syntheticDocument = {
      ...document,
      input_snapshot: { ...document.input_snapshot, sources: [
        { source_id: 'macro', source_tier: 'local_data', as_of: '2026-10-10', used_for: 'financing_conditions' },
        { source_id: 'issuer', source_tier: 'primary', as_of: '2026-10-12', ref: 'https://example.invalid/issuer', used_for: 'debt_maturity' },
        { source_id: 'external', source_tier: 'secondary', as_of: '2026-10-13', ref: 'https://example.invalid/estimate', used_for: 'reference_only' },
      ] },
      findings: { ...document.findings, value_gap_explanation: '借換費用を織り込んだ自前Baseと外部目標の差は対象期不明で比較保留', hypotheses: [
        { statement: '金利上昇が借換費用を増す', result: 'supported', reason: '満期開示から個社cashへ伝達', source_ids: ['macro', 'issuer'] },
        { statement: '外部目標は自前Baseより高い', result: 'unresolved', reason: '対象期と株式basisが未確認のため参考', source_ids: ['external'] },
      ], source_ids: ['macro', 'issuer', 'external'] },
      investment_case: { ...document.investment_case, explanation: '継続収益と負債管理が株主に帰属するcashを生む', status: 'uncertain', status_reason: '借換条件は未取得', source_ids: ['issuer'] },
      valuation: { ...document.valuation, base: { ...document.valuation.base, calculation: '借換費用を控除したBase', source_ids: ['macro', 'issuer'] }, downside: { ...document.valuation.downside, calculation: '追加利払いと遅延を反映したDownside', source_ids: ['macro', 'issuer'] } },
    }
    const syntheticItem: InvestmentProposalItemView = { ...item, action: 'defer', rationale: '借換条件が未取得のため判断保留', terms: null, order_readiness: 'not_applicable', blockers: [{ code: 'material_unknown', message: '借換条件を確認する' }], resume_conditions: ['借換条件の一次資料を入手する'], assessment_projection: { ...item.assessment_projection, raw: syntheticDocument }, assessment_review: { ...item.assessment_review, strongest_countercase: '借換条件悪化でcashが残らない', review_scope: '満期の原典を独立確認し外部目標の対象期は未確認', checked_source_ids: ['issuer', 'external'] } }
    const before = JSON.stringify(syntheticItem)
    const html = render(ProposalAssessmentDetails, syntheticItem)
    for (const phrase of ['価値を生む仮説', '観測と個社への伝達', '条件付き価値と時間', '反証と独立Review', '対象期と株式basisが未確認のため参考', '借換費用を控除したBase', '追加利払いと遅延を反映したDownside', '借換条件悪化でcashが残らない']) expect(html).toContain(phrase)
    expect(html.indexOf('観測と個社への伝達')).toBeLessThan(html.indexOf('条件付き価値と時間'))
    expect(html.indexOf('条件付き価値と時間')).toBeLessThan(html.indexOf('反証と独立Review'))
    expect(html).toContain('href="https://example.invalid/estimate"')
    expect(html).toContain('external · 保存sourceのas_of 2026-10-13')
    expect(html).toContain('所見全体の参照:')
    const businessField = html.match(/<dl[^>]*><dt[^>]*>事業の収益構造<\/dt><dd[^>]*>(.*?)<\/dd><\/dl>/)?.[1]
    expect(businessField).toContain('保存した収益構造')
    expect(businessField).not.toContain('external')
    expect(html.indexOf('所見全体の参照:')).toBeGreaterThan(html.indexOf('事業の収益構造'))
    expect(html.indexOf('所見全体の参照:')).toBeLessThan(html.indexOf('観測と個社への伝達'))
    expect(render(ProposalItemContent, syntheticItem)).toContain('判断保留')
    expect(JSON.stringify(syntheticItem)).toBe(before)
  })
  it('検証仮説がない場合は空見出しを出さず、所見全体の参照と未解決の問いの範囲を保つ', () => {
    const withoutHypotheses = { ...document, findings: { ...document.findings, source_ids: ['issuer'], hypotheses: [], unresolved_questions: [] }, input_snapshot: { ...document.input_snapshot, sources: [{ source_id: 'issuer', as_of: '2026-10-12' }] } }
    const assessmentItem = { ...item, assessment_projection: { ...item.assessment_projection, raw: withoutHypotheses } }
    const html = render(ProposalAssessmentDetails, assessmentItem)
    expect(html).toContain('所見全体の参照: <span>issuer · 保存sourceのas_of 2026-10-12</span>')
    expect(html).not.toContain('観測と個社への伝達')
    expect(html).not.toContain('未解決の問い')

    const onlyQuestion = { ...withoutHypotheses, findings: { ...withoutHypotheses.findings, unresolved_questions: ['借換条件が未取得'] } }
    const questionHtml = render(ProposalAssessmentDetails, { ...assessmentItem, assessment_projection: { ...assessmentItem.assessment_projection, raw: onlyQuestion } })
    expect(questionHtml).toContain('借換条件が未取得')
    expect(questionHtml).toContain('aria-label="残る確認事項"')
    expect(questionHtml).not.toContain('観測と個社への伝達')
  })
  it('参照Review本文が欠けたときは反証節でも表示不足を明示する', () => {
    const html = render(ProposalAssessmentDetails, { ...item, assessment_review: null })
    expect(html).toContain('独立Review本文を表示できません。保存した評価版のReviewを確認してください。')
    expect(html).toContain('参照した Review 本文を表示できません。保存 ID から確認してください。')
    expect(html).not.toContain('独立Reviewの最も強い反対仮説')
  })
  it('価値未解決をBase/Downsideの架空表示へ変換しない', () => {
    const unresolved = { ...item, action: 'defer' as const, terms: null, order_readiness: 'not_applicable' as const, assessment_projection: { ...item.assessment_projection, raw: { ...document, valuation: { status: 'unresolved', unresolved_reason: '借換条件が未取得', base: null, downside: null, horizon_months: null, required_annual_return_pct: null } }, projections: {} } }
    const html = render(ProposalAssessmentDetails, unresolved)
    expect(html).toContain('借換条件が未取得')
    expect(html).not.toContain('分配後の価値')
    expect(html).not.toContain('要求年率 未確認')
  })
  it.each([
    { name: '日次終値', price: 1100, date: '2026-10-13', time: null, basis: 'last_close_unadjusted' },
    { name: '時刻付きquote', price: 1100, date: '2026-10-14', time: '2026-10-14T10:05:00+09:00', basis: 'realtime' },
    { name: '価格欠測', price: null, date: null, time: null, basis: null },
  ])('$nameの価格基準日と実観測時刻を区別する', ({ price, date, time, basis }) => {
    const raw = price === null ? { ...document, valuation: { status: 'unresolved', unresolved_reason: '価格未取得', market_price_fact_id: null, horizon_months: null, required_annual_return_pct: null, base: null, downside: null } } : document
    const html = render(ProposalAssessmentDetails, { ...item, assessment_projection: { ...item.assessment_projection, raw, original_price_yen: price, original_price_as_of: date, original_quote_at: time, original_price_basis: basis, projections: price === null ? {} : item.assessment_projection?.projections } })
    // Inspect the main display, independently of the raw document disclosure.
    const priceLine = html.match(/<p[^>]*>評価起点 .*?<\/p>/)?.[0] ?? ''
    expect(priceLine).toContain('評価起点 2026-10-14')
    expect(priceLine).toContain(`価格基準日 ${date ?? '未確認'}`)
    expect(priceLine).toContain(`原評価の観測価格 ${price ?? '未確認'} 円`)
    if (time) expect(priceLine).toContain(`観測時刻 ${time}`)
    else expect(priceLine).not.toContain('観測時刻')
    if (price === null) {
      expect(html).toContain('価格未取得')
      expect(html).not.toContain('分配後の価値')
      expect(priceLine).not.toContain('価格基準日 2026-10-14')
    }
  })
  it('案件別の保存要求率を比較目安へ置換せず表示する', () => {
    const saved = { ...item, assessment_projection: { ...item.assessment_projection, raw: { ...document, valuation: { ...document.valuation, required_annual_return_pct: '3' } } } }
    const before = JSON.stringify(saved)
    const html = render(ProposalAssessmentDetails, saved)
    expect(html).toContain('要求年率 3%')
    expect(html).not.toContain('要求年率 6%')
    expect(JSON.stringify(saved)).toBe(before)
  })
  it('保存limit/quantity/target_session/warningsを示し、注文や約定と区別する', () => {
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
    for (const text of ['現在 quote での再試算', '試算価格 1100円', '保存 limit の再検証', '現在資金等での試算数量 200株', 'cashが変化しました', '保存した価格・数量・対象日を更新していません']) expect(html).toContain(text)
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
