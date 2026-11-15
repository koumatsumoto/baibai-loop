import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { SavedHoldItemView, SavedHoldQuoteComparisonView } from '../src/api/types'
import { ProposalItemCard } from '../src/pages/ProposalPage'
import { buyFixture, holdFixture, sellFixture } from './fixtures/proposals'

const comparison: SavedHoldQuoteComparisonView = {
  price_context: 'saved_hold_quote',
  calculation_price_yen: '1000.0000000000000001',
  price_as_of: '2026-10-26',
  price_basis: 'last_close_unadjusted',
  horizon_months: 12,
  return_basis: 'conditional_pretax_without_reinvestment',
  base: { total_value_per_share_yen: '1100', total_return_pct: '10.0000', annualized_return_pct: '10.0000' },
  downside: { total_value_per_share_yen: '700', total_return_pct: '-30.0000', annualized_return_pct: '-30.0000' },
}
const item: SavedHoldItemView = {
  ...holdFixture,
  holding_basis: 'sufficient_remaining_reward',
  remaining_reward: { status: 'sufficient', reason: '損失経路と他案を比べた合成の十分性判断。' },
  return_calculation_unavailable_reason: null,
  saved_quote: { price_yen: comparison.calculation_price_yen, price_as_of: comparison.price_as_of, price_basis: comparison.price_basis },
  saved_hold_quote_comparison: comparison,
  assessment: { ...holdFixture.assessment!, valuation: {
    status: 'resolved', price_yen: '1100', price_as_of: '2026-10-25', price_observed_at: null, price_basis: 'last_close_unadjusted',
    horizon_months: 12, required_annual_return_pct: '6',
    base: { terminal_value_per_share_yen: '1100', cash_distribution_per_share_yen: '0', annualized_return_pct: '0.0000', calculation: '合成の元評価価格による年率。', source_ids: [], sources: [] },
    downside: { terminal_value_per_share_yen: '700', cash_distribution_per_share_yen: '0', annualized_return_pct: '-36.3636', calculation: '合成の下方。', source_ids: [], sources: [] },
  } },
}
const render = (value: SavedHoldItemView) => renderToStaticMarkup(createElement(ProposalItemCard, { item: value }))

describe('十分性を確認した保存 hold の終値比較', () => {
  it('元評価価格・保存終値と今後の条件付き見返りを分ける', () => {
    const html = render(item)
    for (const text of ['保存終値で確認した保有の見返り', '1000.0000000000000001 円', '10.0000 %', '-30.0000 %', '12 か月', '2026-10-26', '保存終値に対する条件付き年率', '評価に用いた株価', '1100 円', '保有継続の根拠：十分な残存見返り']) expect(html).toContain(text)
    expect(html).toContain('購入原価からの損益や売却できる価格とは別')
    expect(html).toContain('十分性は保存された作者の比較判断')
    expect(html).toContain('投資家税・売買費用・配当再投資は含みません')
    expect(html).toContain('数値だけで保有や売却へ変更しません')
  })
  it.each(['null', 'absent'] as const)('旧表示 %s を十分性確認や0へ補完しない', mode => {
    const value = { ...holdFixture, saved_hold_quote_comparison: null }
    if (mode === 'absent') delete (value as Partial<SavedHoldItemView>).saved_hold_quote_comparison
    const html = render(value)
    expect(html).toContain('仮説維持だけの保有・条件不足')
    expect(html).toContain('見返り0や十分な収益率へ補っていません')
    expect(html).not.toContain('保存終値に対する条件付き年率')
    expect(html).toContain('現在の収益率が十分なことは別')
  })
  it('0価値の全損失を欠測へ置き換えない', () => {
    const html = render({ ...item, saved_hold_quote_comparison: { ...comparison, downside: { total_value_per_share_yen: '0', total_return_pct: '-100.0000', annualized_return_pct: '-100.0000' } } })
    expect(html).toContain('-100.0000 %')
    expect(html).toContain('>0 円<')
  })
  it('文字列をHTMLにせず精度と末尾を保持する', () => {
    const html = render({ ...item, saved_hold_quote_comparison: { ...comparison, calculation_price_yen: '<script>escape</script>末尾' } })
    expect(html).toContain('&lt;script&gt;escape&lt;/script&gt;末尾')
    expect(html).not.toContain('<script>escape')
  })
  it('buyとsellへhold比較を追加しない', () => {
    for (const value of [buyFixture, sellFixture]) expect(renderToStaticMarkup(createElement(ProposalItemCard, { item: value }))).not.toContain('保存終値で確認した保有の見返り')
  })
})
