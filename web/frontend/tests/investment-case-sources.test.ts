import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { SavedAssessmentView } from '../src/api/types'
import { ProposalItemCard } from '../src/pages/ProposalPage'
import { assessmentFixture, buyFixture, holdFixture, unassessedDeferFixture } from './fixtures/proposals'

function assessment(label = 'current'): SavedAssessmentView {
  return { ...assessmentFixture(label),
    investment_case_source_ids: [`${label}-case-only`],
    investment_case_sources: [{
      source_id: `${label}-case-only`, source_tier: 'primary',
      ref: `https://example.com/${label}-case.pdf`, provider: null, dataset: null,
      as_of: '2026-10-10', retrieved_at: '2026-10-11T12:34:56+09:00',
      used_for: `${label}仮説を検討。<script>未確定</script>\n末尾の留保。`,
    }],
  }
}
function render(pair: SavedAssessmentView) {
  return renderToStaticMarkup(createElement(ProposalItemCard, { item: { ...buyFixture, assessment: pair } }))
}

describe('投資理由区画の保存引用', () => {
  it('専用引用・時計・全文を示し、引用を支持や確認済みへ変更しない', () => {
    const pair = assessment()
    const before = JSON.stringify(pair)
    const html = render(pair)
    for (const text of ['投資理由の保存引用', 'current-case-only', 'https://example.com/current-case.pdf', '2026-10-10', '2026-10-11T12:34:56+09:00', '末尾の留保。', '各文の裏付けや仮説維持の確認、独立した支持票を意味しません', '部分確認', '買付（指値） · buy']) expect(html).toContain(text)
    expect(html).toContain('&lt;script&gt;未確定&lt;/script&gt;')
    expect(html).not.toContain('<script>')
    expect(JSON.stringify(pair)).toBe(before)
  })

  it.each(['absent', 'null'] as const)('旧表示の未収録は他区画の出典で補わない: %s', mode => {
    const pair = assessment()
    pair.value_gap = { explanation: '合成の価値差', findings_source_ids: ['other-section'], findings_sources: [] }
    if (mode === 'absent') {
      Reflect.deleteProperty(pair, 'investment_case_source_ids')
      Reflect.deleteProperty(pair, 'investment_case_sources')
    } else {
      pair.investment_case_source_ids = null
      pair.investment_case_sources = null
    }
    const html = render(pair)
    const section = html.split('投資理由の保存引用')[1].split('評価を更新した根拠')[0]
    expect(section).toContain('引用対応が含まれていません。根拠がないことを示しません。')
    expect(section).not.toContain('current-case-only')
    expect(section).not.toContain('other-section')
  })

  it('当初と今回の引用対応を別に保持する', () => {
    const item = { ...holdFixture, original_assessment: assessment('original'), assessment: assessment('current') }
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item }))
    expect(html.match(/投資理由の保存引用/g)).toHaveLength(2)
    for (const label of ['original', 'current']) {
      const section = html.split(`${label}の投資理由。`)[1].split('評価を更新した根拠')[0]
      expect(section).toContain(`https://example.com/${label}-case.pdf`)
      expect(section).not.toContain(`https://example.com/${label === 'original' ? 'current' : 'original'}-case.pdf`)
    }
    expect(html).toContain('保有継続 · hold')
  })

  it('参照IDだけが残る場合は欠測を示し、別区画を補完しない', () => {
    const html = render({ ...assessment(), investment_case_sources: [] })
    const section = html.split('投資理由の保存引用')[1].split('評価を更新した根拠')[0]
    expect(section).toContain('current-case-only')
    expect(section).toContain('参照先の表示情報なし')
    expect(section).not.toContain('https://example.com/')
  })

  it('未評価deferに投資理由や引用を生成しない', () => {
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item: unassessedDeferFixture }))
    expect(html).toContain('判断保留 · defer')
    expect(html).not.toContain('投資理由の保存引用')
  })
})
