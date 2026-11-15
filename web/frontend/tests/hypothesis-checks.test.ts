import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { SavedAssessmentView } from '../src/api/types'
import { ProposalItemCard } from '../src/pages/ProposalPage'
import { assessmentFixture, buyFixture, holdFixture, unassessedDeferFixture } from './fixtures/proposals'

function assessment(label = 'current'): SavedAssessmentView {
  return { ...assessmentFixture(label),
    hypothesis_checks: [
      { statement: `${label}の仮説：売上増なら利益率も改善`, origin: 'user', result: 'refuted', reason: `${label}の反証理由。<script>未確認</script>\n${'比較期間は同じ。'.repeat(30)}末尾の留保。`, source_ids: [`${label}-hypothesis-only`] },
      { statement: `${label}の売上増加という観測`, origin: 'research', result: 'supported', reason: '当該期間の観測であり持続利益ではない。', source_ids: [`${label}-hypothesis-only`] },
      { statement: `${label}の将来回収`, origin: 'research', result: 'unresolved', reason: '回収時期は未確認。', source_ids: [] },
    ],
    hypothesis_sources: [{
      source_id: `${label}-hypothesis-only`, source_tier: 'primary',
      ref: `https://example.com/${label}-hypothesis.pdf`, provider: null, dataset: null,
      as_of: '2026-10-10', retrieved_at: '2026-10-11T12:34:56+09:00',
      used_for: `${label}の個別命題への引用。`,
    }],
  }
}
function render(pair: SavedAssessmentView) {
  return renderToStaticMarkup(createElement(ProposalItemCard, { item: { ...buyFixture, assessment: pair } }))
}
function section(html: string) {
  return html.split('個別仮説の検討結果')[1].split('評価を更新した根拠')[0]
}

describe('保存評価の個別仮説', () => {
  it('支持/反証/未確認を全文・元順序・専用引用のまま総合採否から分ける', () => {
    const pair = assessment()
    const before = JSON.stringify(pair)
    const html = render(pair)
    for (const text of ['支持（supported）', '反証（refuted）', '未確認（unresolved）', '作者記録の起点：ユーザー（user）', '作者記録の起点：調査（research）', 'https://example.com/current-hypothesis.pdf', '2026-10-10', '2026-10-11T12:34:56+09:00', '末尾の留保。', '総合的な採否・Reviewerの結論・現在の事実と同一ではなく', 'この命題に記録された引用はありません。', '部分確認', '買付（指値） · buy']) expect(html).toContain(text)
    expect(html.indexOf('反証（refuted）')).toBeLessThan(html.indexOf('支持（supported）', html.indexOf('個別仮説の検討結果')))
    expect(html).toContain('&lt;script&gt;未確認&lt;/script&gt;')
    expect(html).not.toContain('<script>')
    expect(JSON.stringify(pair)).toBe(before)
  })

  it.each(['absent', 'null'] as const)('旧表示を総合支持/反対論から補わない: %s', mode => {
    const pair = assessment()
    pair.strongest_countercase = '別区画の強い反対論'
    if (mode === 'absent') {
      Reflect.deleteProperty(pair, 'hypothesis_checks')
      Reflect.deleteProperty(pair, 'hypothesis_sources')
    } else {
      pair.hypothesis_checks = null
      pair.hypothesis_sources = null
    }
    const html = section(render(pair))
    expect(html).toContain('検討記録が含まれていません。反証や未確認事項がないことを示しません。')
    expect(html).not.toContain('別区画の強い反対論')
    expect(html).not.toContain('currentの仮説')
  })

  it('明示的な空リストを旧未収録と区別する', () => {
    const html = section(render({ ...assessment(), hypothesis_checks: [], hypothesis_sources: [] }))
    expect(html).toContain('この保存評価には個別仮説の記録がありません。全論点の確認済みを意味しません。')
    expect(html).not.toContain('検討記録が含まれていません')
  })

  it('当初と今回の仮説の回答を入れ替えない', () => {
    const item = { ...holdFixture, original_assessment: assessment('original'), assessment: assessment('current') }
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item }))
    expect(html.match(/個別仮説の検討結果/g)).toHaveLength(2)
    for (const label of ['original', 'current']) {
      const part = html.split(`${label}の投資理由。`)[1].split('評価を更新した根拠')[0]
      expect(part).toContain(`${label}の仮説`)
      expect(part).toContain(`https://example.com/${label}-hypothesis.pdf`)
      expect(part).not.toContain(`${label === 'original' ? 'current' : 'original'}の仮説`)
    }
    expect(html).toContain('保有継続 · hold')
  })

  it('仮説本文は残して引用表示の欠測を示し他区画から補完しない', () => {
    const pair = assessment()
    pair.hypothesis_sources = null
    pair.investment_case_sources = assessment().hypothesis_sources
    const html = section(render(pair))
    expect(html).toContain('currentの仮説')
    expect(html).toContain('current-hypothesis-only')
    expect(html).toContain('参照先の表示情報なし')
    expect(html).not.toContain('https://example.com/')
  })

  it('未評価deferに仮説や結果を作らない', () => {
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item: unassessedDeferFixture }))
    expect(html).toContain('判断保留 · defer')
    expect(html).not.toContain('個別仮説の検討結果')
  })
})
