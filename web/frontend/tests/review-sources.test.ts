import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { SavedAssessmentView } from '../src/api/types'
import { ProposalItemCard } from '../src/pages/ProposalPage'
import { assessmentFixture, buyFixture, holdFixture, unassessedDeferFixture } from './fixtures/proposals'

function assessment(label = 'current'): SavedAssessmentView {
  const pair = assessmentFixture(label)
  pair.review.checked_source_ids = [`${label}-review-only`, `${label}-price`]
  pair.review.checked_sources = [
    { source_id: `${label}-price`, source_tier: 'local_data', ref: null, provider: 'synthetic', dataset: 'price', as_of: '2026-10-09', retrieved_at: '2026-10-10T12:00:00+09:00', used_for: '合成の価格確認資料' },
    { source_id: `${label}-review-only`, source_tier: 'secondary', ref: `https://example.com/${label}-review.html`, provider: null, dataset: null, as_of: '2026-10-10', retrieved_at: '2026-10-11T12:34:56+09:00', used_for: `${label}確認対象。<script>未確定</script>\n末尾の限定。` },
  ]
  pair.investment_case_source_ids = [`${label}-author-only`]
  pair.investment_case_sources = []
  return pair
}
function render(pair: SavedAssessmentView) {
  return renderToStaticMarkup(createElement(ProposalItemCard, { item: { ...buyFixture, assessment: pair } }))
}
function reviewSection(html: string) {
  return html.split('独立検証の保存確認資料')[1].split('作者の最も強い反対論')[0]
}

describe('独立Reviewの保存確認資料', () => {
  it('作者引用と分け、確認対象の順序・時計・留保を保持する', () => {
    const pair = assessment()
    const before = JSON.stringify(pair)
    const html = render(pair)
    const section = reviewSection(html)
    for (const text of ['current-review-only', 'current-price', 'https://example.com/current-review.html', '2026-10-10', '2026-10-11T12:34:56+09:00', '末尾の限定。', '全記述の検証・資料間の独立性・現在の安全性を保証しません']) expect(section).toContain(text)
    expect(section.indexOf('current-review-only')).toBeLessThan(section.indexOf('current-price'))
    expect(section).not.toContain('current-author-only')
    expect(section).toContain('&lt;script&gt;未確定&lt;/script&gt;')
    expect(html).not.toContain('<script>')
    expect(html).toContain('部分確認')
    expect(html).toContain('買付（指値） · buy')
    expect(JSON.stringify(pair)).toBe(before)
  })

  it.each(['absent', 'null'] as const)('旧表示の未収録を作者引用から補わない: %s', mode => {
    const pair = assessment()
    if (mode === 'absent') {
      Reflect.deleteProperty(pair.review, 'checked_source_ids')
      Reflect.deleteProperty(pair.review, 'checked_sources')
    } else {
      pair.review.checked_source_ids = null
      pair.review.checked_sources = null
    }
    const section = reviewSection(render(pair))
    expect(section).toContain('確認資料の対応が含まれていません。未確認や確認済みとは補いません。')
    expect(section).not.toContain('current-review-only')
    expect(section).not.toContain('current-author-only')
  })

  it('当初と今回の検証対象を別々に表示する', () => {
    const item = { ...holdFixture, original_assessment: assessment('original'), assessment: assessment('current') }
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item }))
    expect(html.match(/独立検証の保存確認資料/g)).toHaveLength(2)
    for (const label of ['original', 'current']) {
      const section = reviewSection(html.split(`${label}の検証範囲`)[1])
      expect(section).toContain(`https://example.com/${label}-review.html`)
      expect(section).not.toContain(`https://example.com/${label === 'original' ? 'current' : 'original'}-review.html`)
    }
    expect(html).toContain('保有継続 · hold')
  })

  it('IDだけの場合は参照先不足を保ち、作者の同ID資料も転用しない', () => {
    const pair = assessment()
    pair.investment_case_source_ids = pair.review.checked_source_ids
    pair.investment_case_sources = pair.review.checked_sources
    pair.review.checked_sources = []
    const section = reviewSection(render(pair))
    expect(section).toContain('current-review-only')
    expect(section).toContain('参照先の表示情報なし')
    expect(section).not.toContain('https://example.com/')
  })

  it('空配列を未記録や一次資料verifiedへ変換しない', () => {
    const pair = assessment()
    pair.review.checked_source_ids = []
    pair.review.checked_sources = []
    const html = render(pair)
    expect(reviewSection(html)).toContain('確認資料は空です。別の引用から補っていません。')
    expect(html).toContain('部分確認')
  })

  it('未評価deferへReviewや資料を作らない', () => {
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item: unassessedDeferFixture }))
    expect(html).toContain('判断保留 · defer')
    expect(html).not.toContain('独立検証の保存確認資料')
  })
})
