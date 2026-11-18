import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { SavedPriceValueView } from '../src/api/types'
import { ProposalItemCard } from '../src/pages/ProposalPage'
import { assessmentFixture, buyFixture, unassessedDeferFixture } from './fixtures/proposals'

const finding: SavedPriceValueView = {
  status: 'not_supported_at_quote', acquisition_limit: null,
  observed_quote: { fact_id: 'q', price_yen: '100', as_of: '2026-10-22', observed_at: null, price_basis: 'last_close_unadjusted', source_ids: [] },
  price_compatible_conditions: '価格が織り込む回収の条件。', price_only_condition: { explanation: '同一権利のまま。', ceiling_yen: '75' },
  value_facts: [{ fact_id: 'right', value: '確定した受取の権利', unit: '条項', as_of: '2026-09-15', source_ids: [] }], sources: [],
}

function render(f: SavedPriceValueView | null) {
  return renderToStaticMarkup(createElement(ProposalItemCard, { item: { ...buyFixture, assessment: { ...assessmentFixture('当時'),price_value_case: f } } }))
}

describe('評価時の一株の割安と元手の行動', () => {
  it('買いの行動でも元価格は不支持・75円の価格条件待ち。現在値や指値に付け替えない', () => {
    const html = render(finding)
    expect(html).toContain('資金の判断：買付（指値）')
    expect(html).toContain('この価格では支持せず、価格条件を待つ')
    expect(html).toContain('条件上限 75 円／株。注文指値ではありません')
    expect(html).toContain('確定した受取の権利')
    expect(html).toContain('dateTime="2026-10-22"')
    expect(html.indexOf('価格条件を待つ')).toBeLessThan(html.indexOf('data-proposal-details'))
  })
  it('給付権を取得できる最終立会を入金日や現在の保有へ転記しない', () => {
    const known = render({ ...finding, acquisition_limit: { value_fact_id: 'right', last_eligible_trade_date: '2026-10-26' } })
    expect(known).toContain('最後に取得できる立会')
    expect(known).toContain('入金日ではありません')
    const unknown = render({ ...finding, acquisition_limit: { value_fact_id: 'right', last_eligible_trade_date: null } })
    expect(unknown).toContain('未確認')
  })
  it('価格待ち・単なる不支持・未解決・旧記録の欠欄・評価自体の不在を取り違えない', () => {
    expect(render({ ...finding, price_only_condition: { explanation:'価格条件あり',ceiling_yen:null } })).toContain('金額の指定なし')
    expect(render({ ...finding, price_only_condition:null })).toContain('この価格では割安を支持しない')
    expect(render({ ...finding, status:'undetermined', observed_quote:null, price_only_condition:null })).toContain('価値と価格の比較は未確定')
    expect(render(null)).toContain('明示的な割安判断がありません')
    expect(renderToStaticMarkup(createElement(ProposalItemCard, {item:unassessedDeferFixture}))).toContain('確認済みの評価はありません')
  })
})
