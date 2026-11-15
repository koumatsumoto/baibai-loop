import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { SavedHoldItemView, SavedHoldingPositionView, SavedSellItemView } from '../src/api/types'
import { ProposalItemCard } from '../src/pages/ProposalPage'
import { buyFixture, holdFixture, sellFixture } from './fixtures/proposals'

const position: SavedHoldingPositionView = {
  quantity: 200,
  quantity_basis_confirmed: true,
  cost_yen: '90000.0000000000000001',
  inputs_checked_at: '2026-10-27T16:10:00+09:00',
  ledger_as_of: '2026-10-26T17:00:00+09:00',
}
const render = (item: SavedHoldItemView | SavedSellItemView) => renderToStaticMarkup(createElement(ProposalItemCard, { item }))

describe('保有判断の保存数量・原価と確認状態', () => {
  it.each([holdFixture, sellFixture])('$action の保存規模と時計を将来見返りから分ける', item => {
    const html = render({ ...item, saved_position: position })
    for (const text of ['判断対象の保存保有', '200 株', '保存時に確認済み', '90000.0000000000000001 円', position.inputs_checked_at, position.ledger_as_of, '原価だけで保有や売却を決めません']) expect(html).toContain(text)
    expect(html).toContain('現在保有・注文可能数量ではなく')
    expect(html).toContain('原価は現在時価・回収下限・今後の見返りの分母ではなく')
  })
  it('未確認数量を0や注文可能量にせず条件未確定sellを維持する', () => {
    const html = render({ ...sellFixture, saved_position: { ...position, quantity_basis_confirmed: false } })
    expect(html).toContain('200 株')
    expect(html).toContain('未確認。注文数量として使用しない')
    expect(html).toContain('具体条件未確定')
    expect(html).not.toContain('保存時に確認済み')
  })
  it.each(['absent', 'null'] as const)('旧表示 %s を保有なしや0円へ補わない', mode => {
    for (const item of [holdFixture, sellFixture]) {
      const value = { ...item, saved_position: null }
      if (mode === 'absent') delete (value as Partial<SavedHoldItemView>).saved_position
      const html = render(value)
      expect(html).toContain('保有なし・0円・確認済みへ補っていません')
      expect(html).not.toContain('保存された保有数量')
    }
  })
  it('0原価と数値文字列の末尾を保持しHTMLとして解釈しない', () => {
    expect(render({ ...holdFixture, saved_position: { ...position, cost_yen: '0' } })).toContain('>0 円<')
    const html = render({ ...holdFixture, saved_position: { ...position, cost_yen: '<script>bad</script>末尾' } })
    expect(html).toContain('&lt;script&gt;bad&lt;/script&gt;末尾')
    expect(html).not.toContain('<script>bad')
  })
  it('未保有buyへ保有規模を足さない', () => {
    expect(renderToStaticMarkup(createElement(ProposalItemCard, { item: buyFixture }))).not.toContain('判断対象の保存保有')
  })
})
