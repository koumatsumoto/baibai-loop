import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { SavedDeferItemView, SavedHoldingPositionView } from '../src/api/types'
import { ProposalItemCard } from '../src/pages/ProposalPage'
import { reviewedDeferFixture, unassessedDeferFixture } from './fixtures/proposals'

const position: SavedHoldingPositionView = {
  quantity: 100,
  quantity_basis_confirmed: true,
  cost_yen: '90000.0000000000000001',
  inputs_checked_at: '2026-10-27T16:10:00+09:00',
  ledger_as_of: '2026-10-26T17:00:00+09:00',
}
const render = (item: SavedDeferItemView) => renderToStaticMarkup(createElement(ProposalItemCard, { item }))

describe('判断保留にも残る保存保有', () => {
  it.each([reviewedDeferFixture, unassessedDeferFixture])('評価有無と独立に保存数量・原価・時計を残す', item => {
    const html = render({ ...item, saved_position: position })
    for (const text of ['判断対象の保存保有', '100 株', '90000.0000000000000001 円', position.inputs_checked_at, position.ledger_as_of, '判断を保留した不足理由', item.resume_conditions[0]]) expect(html).toContain(text)
    expect(html).toContain('判断保留は、保有なし・売却済み・保有継続の推奨を意味しません')
    expect(html).toContain('現在保有・注文可能数量ではなく')
    expect(html).not.toContain('保有継続の根拠：')
    expect(html).not.toContain('売却の根拠：')
    expect(html).not.toContain('保存売却下限と保有継続の比較')
  })
  it('未確認数量を注文可能量や0へ補わない', () => {
    const html = render({ ...unassessedDeferFixture, saved_position: { ...position, quantity_basis_confirmed: false } })
    expect(html).toContain('100 株')
    expect(html).toContain('未確認。注文数量として使用しない')
    expect(html).not.toContain('保存時に確認済み')
  })
  it.each(['absent', 'null'] as const)('旧 %s から保有や売却済みを推定しない', mode => {
    const item = { ...reviewedDeferFixture, saved_position: null }
    if (mode === 'absent') delete (item as Partial<SavedDeferItemView>).saved_position
    const html = render(item)
    expect(html).not.toContain('判断対象の保存保有')
    expect(html).not.toContain('保存された保有数量')
    expect(html).toContain('判断保留は、保有なし・売却済み・保有継続の推奨を意味しません')
    expect(html).toContain(item.blockers[0].message)
  })
  it('原価文字列を切らずHTMLとして解釈しない', () => {
    const html = render({ ...reviewedDeferFixture, saved_position: { ...position, cost_yen: '<script>bad</script>末尾' } })
    expect(html).toContain('&lt;script&gt;bad&lt;/script&gt;末尾')
    expect(html).not.toContain('<script>bad')
  })
})
