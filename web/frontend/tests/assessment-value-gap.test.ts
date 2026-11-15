import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { AssessmentValueGap } from '../src/components/AssessmentValueGap'
import { ProposalItemCard } from '../src/pages/ProposalPage'
import { assessmentFixture, buyFixture, holdFixture, unassessedDeferFixture } from './fixtures/proposals'

function fixture() {
  const assessment = assessmentFixture('value-gap')
  assessment.value_gap = {
    explanation: `${'外部の対象期は未確認。'.repeat(40)}\n末尾の留保：会社予想の再利用を独立票とせず、自身の追加資本仮定と区別する。`,
    findings_source_ids: ['issuer', 'missing'],
    findings_sources: [{ source_id: 'issuer', source_tier: 'primary', ref: 'https://example.test/ir?version=old&x=1', provider: null, dataset: null, as_of: '2026-08-31', retrieved_at: '2026-10-20T03:00:00Z', used_for: '調査所見の一般根拠。外部予想と同時点とはしない。' }],
  }
  return assessment
}

describe('価値差の保存説明', () => {
  it('全文と末尾の留保を残し、資料日・取得日を現在の比較確認へ変えない', () => {
    const assessment = fixture()
    const html = renderToStaticMarkup(createElement(AssessmentValueGap, { assessment }))
    expect(html).toContain(assessment.value_gap!.explanation)
    expect(html).toContain('dateTime="2026-08-31"')
    expect(html).toContain('dateTime="2026-10-20T03:00:00Z"')
    expect(html).toContain('dateTime="2026-10-26"')
    expect(html).toContain('調査所見全体の参照資料')
    expect(html).toContain('現在の外部予想との自動比較ではありません')
    expect(html).toContain('参照先の表示情報なし')
  })

  it.each(['missing', 'null'] as const)('旧表示JSONを価値差なしに読み替えない: %s', kind => {
    const assessment = fixture()
    if (kind === 'missing') Reflect.deleteProperty(assessment, 'value_gap')
    else assessment.value_gap = null
    const html = renderToStaticMarkup(createElement(AssessmentValueGap, { assessment }))
    expect(html).toContain('この表示データには価値差の説明が含まれていません。')
    expect(html).not.toContain('価値差なし')
    expect(html).not.toContain('調査所見全体の参照資料')
  })

  it('説明をHTMLにせず、リンクと文章は既存の安全な表示を使う', () => {
    const assessment = fixture()
    assessment.value_gap!.explanation = '<script>bad()</script>\n<b>異なる対象期</b>'
    const html = renderToStaticMarkup(createElement(AssessmentValueGap, { assessment }))
    expect(html).toContain('&lt;script&gt;bad()&lt;/script&gt;\n&lt;b&gt;異なる対象期&lt;/b&gt;')
    expect(html).toContain('href="https://example.test/ir?version=old&amp;x=1"')
    expect(html).not.toContain('<script>')
  })

  it('当初と今回のexact評価を分け、外部観点や行動を合成しない', () => {
    const holding = structuredClone(holdFixture)
    holding.assessment = fixture()
    holding.original_assessment = fixture()
    holding.original_assessment.as_of = '2026-07-01'
    holding.original_assessment.value_gap!.explanation = '当初評価だけの比較条件'
    const before = JSON.stringify(holding)
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item: holding }))
    expect(html).toContain('当初評価だけの比較条件')
    expect(html).toContain('末尾の留保：会社予想の再利用を独立票とせず')
    expect(html).toContain('dateTime="2026-07-01"')
    expect(JSON.stringify(holding)).toBe(before)
    const buy = { ...buyFixture, assessment: fixture() }
    const buyHtml = renderToStaticMarkup(createElement(ProposalItemCard, { item: buy }))
    expect(buyHtml).toContain('買付（指値） · buy')
    expect(buyHtml).toContain('観点別の判断は保存されていません。')
    expect(buyHtml).not.toContain('当初評価だけの比較条件')
  })

  it('未評価の保留に価値差や説明を作らない', () => {
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item: unassessedDeferFixture }))
    expect(html).toContain('判断保留 · defer')
    expect(html).not.toContain('価値と市場評価のずれ')
  })
})
