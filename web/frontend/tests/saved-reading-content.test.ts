import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import type { StockAssessmentDetailView } from '../src/api/types'
import { AssessmentReviewContent } from '../src/components/ReviewedAssessmentContent'
import { ProposalReviewContent } from '../src/pages/TradeProposalPage'
import { researchYenValue } from '../src/lib/research'
import { SavedStockAssessment } from '../src/pages/StockAssessmentPage'

// Synthetic saved pair A and later pair B. No account/store or published research.
function assessment(version: 'A' | 'B'): StockAssessmentDetailView {
  return {
    revision: { ticker: '1234', company_name: '合成企業', sector: '合成業種', summary: '', as_of: '2026-10-14', published_at: '2026-10-14T18:00:00+09:00', assessment_id: `stock-assessment-${version}`, stance: 'uncertain', pmax_original_yen: 900, review_id: `assessment-review-${version}`, status: 'published' },
    core_sha256: version.toLowerCase().repeat(64),
    document: {
      input_snapshot: { as_of: '2026-10-14', sources: [{ source_id: 'issuer', source_tier: 'primary', used_for: '満期と借換条件', as_of: '2026-10-12', retrieved_at: '2026-10-14T09:00:00+09:00', ref: 'https://example.invalid/issuer' }] },
      findings: { business_understanding: `保存された事業理解${version}`, value_gap_explanation: `保存された価値差${version}`, source_ids: ['issuer'], hypotheses: [], unresolved_questions: ['借換条件は未確認'], realization: {} },
      investment_case: { status: 'intact', status_reason: '事業継続を確認', invalidation_conditions: ['主要顧客喪失'] },
      valuation: { status: 'unresolved', unresolved_reason: '当期の資金条件が未確認' },
      conclusion: { stance: 'uncertain', strongest_countercase: `作者の反証${version}` },
    },
    projection: { original_price_yen: 950, original_price_as_of: '2026-10-14', original_price_basis: 'last_close_unadjusted' },
    review: { review_id: `assessment-review-${version}`, reviewer_identity: `独立確認者${version}`, reviewed_at: '2026-10-14T17:00:00+09:00', review_scope: '満期原典を確認、借換条件は未確認', primary_source_check: 'partially_verified', checked_source_ids: ['issuer'], strongest_countercase: `独立反証${version}`, recalculated_projections: [], uncertainty_acceptance_reason: null },
  }
}

function renderSaved(data: StockAssessmentDetailView, search = '') {
  return renderToStaticMarkup(createElement(MemoryRouter, { initialEntries: [`/stocks/stock-assessments/${data.revision.assessment_id}${search}`] }, createElement(SavedStockAssessment, { data })))
}

describe('保存判断の読書表示', () => {
  it('固定Aの結論・価格・Reviewを読み、最新Bへの置換をしない', () => {
    const fixed = assessment('A')
    const latest = assessment('B')
    const before = JSON.stringify(fixed)
    const html = renderSaved(fixed, '?proposal=original')
    for (const phrase of ['保存された価値差A', '保存された事業理解A', '未確定', '原評価の観測価格 950', '独立反証A', '独立確認者A', 'stock-assessment-A', 'assessment-review-A']) expect(html).toContain(phrase)
    expect(html).not.toContain('保存された事業理解B')
    expect(html).not.toContain('assessment-review-B')
    expect(renderSaved(latest)).toContain('保存された事業理解B')
    expect(html).toContain('href="/stocks/investment-proposals/original#proposal-item-1234"')
    expect(html).toContain('最新の銘柄情報・評価を見る（この固定版とは別）')
    expect(html.indexOf('価値と価格の隔たり')).toBeLessThan(html.indexOf('事業の収益構造'))
    expect(html.indexOf('事業の収益構造')).toBeLessThan(html.indexOf('独立検証の確認範囲'))
    expect(html).toContain('借換条件は未確認')
    expect(html).not.toContain('Base</p>')
    expect(html).toContain('保存された再計算値はありません')
    expect(JSON.stringify(fixed)).toBe(before)
  })

  it('JSONは一か所へ畳み、確認範囲・資料状態・用途を通常の文章で読む', () => {
    const html = renderSaved(assessment('A'))
    expect(html.match(/<pre\b/g)).toHaveLength(1)
    for (const phrase of ['独立検証の確認範囲', '一次資料の確認状態', '一部確認', '用途: 満期と借換条件', '受領時刻: 2026-10-14T09:00:00+09:00', '保存JSON']) expect(html).toContain(phrase)
    expect(html).toContain('href="/stocks"')
    expect(html).not.toContain('元の提案・この銘柄の判断に戻る')
  })

  it('独立再計算は保存された値だけを示し、0を欠測に変えない', () => {
    const review = { ...assessment('A').review, recalculated_projections: [{ name: 'base', terminal_value_per_share_yen: '1000', cash_distribution_per_share_yen: '0' }], nonmaterial_unknown_reason: '少額債権の回収時期は価値差を左右しない' }
    const html = renderToStaticMarkup(createElement(AssessmentReviewContent, { review }))
    expect(html).toContain('分配後の価値 1,000 円')
    expect(html).toContain('今後の累積分配 0 円')
    expect(html).toContain('少額債権の回収時期は価値差を左右しない')
    expect(html).not.toContain('Downside')
  })

  it('重要な未知の受容理由を非重要というラベルへ変えない', () => {
    const review = { ...assessment('A').review, uncertainty_acceptance_reason: '顧客別採算は重要な未知。継続実績と条件付き見返りから引き受ける。' }
    const before = JSON.stringify(review)
    const html = renderToStaticMarkup(createElement(AssessmentReviewContent, { review }))
    expect(html).toContain('残る不確実性を引き受ける理由')
    expect(html).toContain('顧客別採算は重要な未知')
    expect(html).not.toContain('非重要とした理由')
    expect(JSON.stringify(review)).toBe(before)
  })

  it('提案内容Reviewのpassを人間の採用や注文成立にしない', () => {
    const review = { review_id: 'proposal-review-original', reviewer_identity: '合成reviewer', reviewed_at: '2026-10-14T18:00:00+09:00', conclusion: 'pass', draft_sha256: 'a'.repeat(64), open_findings: [] }
    const html = renderToStaticMarkup(createElement(ProposalReviewContent, { review, proposalId: 'original', draftSha256: 'a'.repeat(64) }))
    expect(html).toContain('提案内容のReview通過')
    expect(html).toContain('人間の採用・注文・約定を示しません')
    expect(html).toContain('保存された未解決の所見なし')
    expect(html).not.toContain('人間確認済み')
  })
})


describe('狭い画面でも保存本文の意味を保つ', () => {
  it('企業名と条件付きPmax、既知の状態を日本語で表示する', () => {
    const fixed = assessment('A')
    fixed.document.findings = { ...fixed.document.findings as Record<string, unknown>, hypotheses: [{ statement: '金利の伝達', result: 'refuted', reason: '固定負債を確認', source_ids: ['issuer'] }], realization: { timing: 'uncertain' } }
    const before = JSON.stringify(fixed)
    const html = renderSaved(fixed)
    for (const phrase of ['合成企業 · 1234 · 保存した銘柄評価', '条件付き購入上限', '購入の推奨、現在の注文条件を示しません', '維持されている', '反証あり', '時期 未確定', 'wrap-anywhere']) expect(html).toContain(phrase)
    expect(JSON.stringify(fixed)).toBe(before)
  })

  it('長い数値は表示だけ丸め、0と欠測を区別する', () => {
    const exact = '2694.969163526429267164050093'
    expect(researchYenValue(exact)).toBe('2,694.97')
    expect(researchYenValue('0')).toBe('0')
    for (const value of [null, undefined, '', ' ', 'not-a-number', Infinity, {}]) expect(researchYenValue(value)).toBe('未確認')
    expect(exact).toBe('2694.969163526429267164050093')
    const review = { ...assessment('A').review, recalculated_projections: [{ name: 'base', terminal_value_per_share_yen: exact, cash_distribution_per_share_yen: '0' }] }
    const before = JSON.stringify(review)
    const html = renderToStaticMarkup(createElement(AssessmentReviewContent, { review }))
    expect(html).toContain('分配後の価値 2,694.97 円')
    expect(html).toContain('今後の累積分配 0 円')
    expect(html).toContain('表示のみ小数2桁まで')
    expect(html).toContain('wrap-anywhere')
    expect(JSON.stringify(review)).toBe(before)
  })
})
