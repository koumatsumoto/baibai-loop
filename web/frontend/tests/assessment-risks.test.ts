import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { SavedAssessmentView } from '../src/api/types'
import { AssessmentRisks } from '../src/components/AssessmentRisks'
import { ProposalItemCard } from '../src/pages/ProposalPage'
import { assessmentFixture, buyFixture, holdFixture, unassessedDeferFixture } from './fixtures/proposals'

function fixture(): SavedAssessmentView {
  const assessment = assessmentFixture('cash')
  assessment.risk_analysis = {
    risks: [
      { axis: 'funding_liquidity', assessment: 'acceptable', evidence_status: 'verified', summary: '合成：支払日までの条件付きcash。安全保証ではない。', as_of: '2026-09-30', source_ids: ['ir'] },
      { axis: 'debt_repayment', assessment: 'adverse', evidence_status: 'partially_verified', summary: '2027/7の元本返済と借換は別分岐。借換確約は未確認。', as_of: '2026-10-01', source_ids: ['ir'] },
      { axis: 'cash_flow', assessment: 'unknown', evidence_status: 'unverified', summary: '将来CFは不明。ゼロや確定資金にはしない。\n会社配当支出と新規株主の権利は別。', as_of: '2026-10-02', source_ids: ['missing'] },
    ],
    unresolved_questions: ['同じcashを還元と元本返済へ二重使用していないか。', '追加支出と借換条件は価値仮定へどう届くか。'],
    sources: [{ source_id: 'ir', source_tier: 'primary', ref: 'https://example.com/ir?date=2026-09-30&x=1', provider: null, dataset: null, as_of: '2026-09-30', retrieved_at: '2026-10-20T03:00:00Z', used_for: '合成：期末残高と満期。将来CFの予測ではない。' }],
  }
  return assessment
}

function render(assessment: SavedAssessmentView) {
  return renderToStaticMarkup(createElement(AssessmentRisks, { assessment }))
}

describe('保存リスクと未解決の問い', () => {
  it('判断と根拠確認状態を分け、全記録を省略せず保存時計と共に表示する', () => {
    const assessment = fixture()
    const html = render(assessment)
    for (const risk of assessment.risk_analysis!.risks) expect(html).toContain(risk.summary)
    for (const question of assessment.risk_analysis!.unresolved_questions) expect(html).toContain(question)
    for (const label of ['受容可能（acceptable）', '不利な要因（adverse）', '不明（unknown）', '根拠の確認：確認済み', '根拠の確認：部分確認', '根拠の確認：未確認']) expect(html).toContain(label)
    expect(html).toContain('評価基準日 <time dateTime="2026-10-26"')
    expect(html).toContain('検討基準日 <time dateTime="2026-10-01"')
    expect(html).toContain('取得時刻：<time dateTime="2026-10-20T03:00:00Z"')
    expect(html).toContain('根拠の対象日：<time dateTime="2026-09-30"')
    expect(html).toContain('参照先の表示情報なし')
    expect(html).toContain('問いの有無で自動判定せず')
  })

  it.each(['missing', 'null'] as const)('旧表示JSONの省略をリスク/問いなしへ変えない: %s', kind => {
    const assessment = fixture()
    if (kind === 'missing') Reflect.deleteProperty(assessment, 'risk_analysis')
    else assessment.risk_analysis = null
    const html = render(assessment)
    expect(html).toContain('この表示データにはリスク・未解決の問いが含まれていません。')
    expect(html).not.toContain('この評価に未解決の問いは記録されていません。')
    expect(html).not.toContain('受容可能（acceptable）')
  })

  it('記録した空配列も安全や将来確定へ読み替えない', () => {
    const assessment = fixture()
    assessment.risk_analysis = { risks: [], unresolved_questions: [], sources: [] }
    const html = render(assessment)
    expect(html).toContain('保存された項目はありません。安全という判定ではありません。')
    expect(html).toContain('この評価に未解決の問いは記録されていません。将来の不確実性がないことを示しません。')
    expect(html).not.toContain('この表示データにはリスク・未解決の問いが含まれていません。')
  })

  it('保存文章をHTMLにせず、根拠は既存の表示と同じ方法で読む', () => {
    const assessment = fixture()
    assessment.risk_analysis!.risks[0].summary = '<script>bad()</script>\n条件付き。'
    assessment.risk_analysis!.unresolved_questions = ['<b>未確定</b>']
    const html = render(assessment)
    expect(html).toContain('&lt;script&gt;bad()&lt;/script&gt;\n条件付き。')
    expect(html).toContain('&lt;b&gt;未確定&lt;/b&gt;')
    expect(html).toContain('href="https://example.com/ir?date=2026-09-30&amp;x=1"')
    expect(html).not.toContain('<script>')
  })

  it('buyをunknownで保留へ変えず、旧/現在pairの問いを混ぜない', () => {
    const buy = structuredClone(buyFixture)
    buy.assessment = fixture()
    const before = JSON.stringify(buy)
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item: buy }))
    expect(html).toContain('買付（指値） · buy')
    expect(html).toContain('リスクと未解決の問い')
    expect(html).toContain('不明（unknown）')
    expect(JSON.stringify(buy)).toBe(before)

    const holding = structuredClone(holdFixture)
    holding.assessment = fixture()
    holding.original_assessment = fixture()
    holding.original_assessment.as_of = '2026-07-01'
    holding.original_assessment.risk_analysis!.unresolved_questions = ['当初評価だけの問い']
    const holdingHtml = renderToStaticMarkup(createElement(ProposalItemCard, { item: holding }))
    expect(holdingHtml).toContain('当初評価だけの問い')
    expect(holdingHtml).toContain('同じcashを還元と元本返済へ二重使用していないか。')
    expect(holdingHtml).toContain('dateTime="2026-07-01"')
  })

  it('未評価のdeferに評価リスクを合成しない', () => {
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item: unassessedDeferFixture }))
    expect(html).toContain('判断保留 · defer')
    expect(html).not.toContain('リスクと未解決の問い')
  })
})
