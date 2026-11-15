import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { SavedCashView, SavedProposalView } from '../src/api/types'
import { ProposalReport } from '../src/pages/ProposalPage'
import { newProposalFixture, proposalViewFixture, unassessedDeferFixture } from './fixtures/proposals'

const noActionFixture = newProposalFixture.items.find(item => item.action === 'no_action')!
const cash: SavedCashView = {
  available_cash_yen: '200000.0000000000000001',
  inputs_checked_at: '2026-10-27T16:10:00+09:00',
  ledger_as_of: '2026-10-26T17:00:00+09:00',
}
const render = (proposal: SavedProposalView, ticker?: string) => renderToStaticMarkup(createElement(ProposalReport, { data: proposalViewFixture(proposal), ticker }))

describe('行動を問わない保存cashの比較前提', () => {
  it.each([noActionFixture, unassessedDeferFixture])('買付比較がない場合も元手と元時計を残す', item => {
    const html = render({ ...newProposalFixture, items: [item], saved_cash: cash })
    for (const text of ['比較の出発点となる保存cash', cash.available_cash_yen, cash.inputs_checked_at, cash.ledger_as_of, '総資産や買付後の残cashではなく', '銘柄別に加算しません', '既存の資金拘束は控除済み', '現在の注文可能額ではありません']) expect(html).toContain(text)
    expect(html).toContain(item.rationale)
    expect(html).toContain(newProposalFixture.comparison)
    expect(html).toContain(newProposalFixture.forgone)
    expect(html).not.toContain('保存指値・選択数量の金額比較')
  })
  it('銘柄だけの表示でも共通cashをその銘柄への配分に変えない', () => {
    const html = render({ ...newProposalFixture, saved_cash: cash }, noActionFixture.ticker)
    expect(html).toContain(cash.available_cash_yen)
    expect(html).toContain('保存提案全体に共通する比較前の未拘束額')
    expect(html).toContain('全額投資・現金維持の推奨')
    expect(html.match(/比較の出発点となる保存cash<\/h3>/g)).toHaveLength(1)
  })
  it.each(['absent', 'null'] as const)('旧 %s をゼロやNAVで補わない', mode => {
    const proposal = { ...newProposalFixture, items: [noActionFixture], saved_cash: null }
    if (mode === 'absent') delete (proposal as Partial<SavedProposalView>).saved_cash
    const html = render(proposal)
    expect(html).toContain('この表示データには未拘束cashと確認時刻がありません')
    expect(html).toContain('0円や全体資本から補っていません')
    expect(html).not.toContain('cash入力の収集時刻')
  })
  it('既知ゼロを未記録にせずNAV不明とも区別する', () => {
    const html = render({ ...newProposalFixture, items: [noActionFixture], saved_cash: { ...cash, available_cash_yen: '0' }, portfolio_exposure: { ...newProposalFixture.portfolio_exposure, total_capital_yen: null } })
    expect(html).toContain('>0 円<')
    expect(html).toContain('cash入力の収集時刻')
    expect(html).not.toContain('この表示データには未拘束cashと確認時刻がありません')
  })
  it('金額文字列を丸めずHTMLとして解釈しない', () => {
    const html = render({ ...newProposalFixture, saved_cash: { ...cash, available_cash_yen: '<script>bad</script>末尾' } })
    expect(html).toContain('&lt;script&gt;bad&lt;/script&gt;末尾')
    expect(html).not.toContain('<script>bad')
  })
})
