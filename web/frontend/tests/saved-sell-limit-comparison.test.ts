import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { SavedSellItemView, SavedSellLimitComparisonView } from '../src/api/types'
import { ProposalItemCard } from '../src/pages/ProposalPage'
import { buyFixture, holdFixture, sellFixture } from './fixtures/proposals'

const comparison: SavedSellLimitComparisonView = {
  price_context: 'author_sell_minimum',
  calculation_price_yen: '900.0000000000000001',
  horizon_months: 12,
  return_basis: 'conditional_pretax_without_reinvestment',
  base: { total_value_per_share_yen: '1100', total_return_pct: '22.2222', annualized_return_pct: '22.2222' },
  downside: { total_value_per_share_yen: '700', total_return_pct: '-22.2222', annualized_return_pct: '-22.2222' },
}
const item: SavedSellItemView = {
  ...sellFixture,
  reason_code: 'insufficient_remaining_reward',
  rationale: '合成の観測終値では見返り不足。下限で保有する機会費用は別に比較した。',
  assessment: { ...buyFixture.assessment!, valuation: {
    status: 'resolved', price_yen: '1000', price_as_of: '2026-10-23', price_observed_at: null,
    price_basis: 'last_close_unadjusted', horizon_months: 12, required_annual_return_pct: '6',
    base: { terminal_value_per_share_yen: '1100', cash_distribution_per_share_yen: '0', annualized_return_pct: '10', calculation: '合成の中心ケース。', source_ids: [], sources: [] },
    downside: { terminal_value_per_share_yen: '700', cash_distribution_per_share_yen: '0', annualized_return_pct: '-30', calculation: '合成の下方ケース。', source_ids: [], sources: [] },
  } },
  saved_quote: buyFixture.saved_quote,
  terms: { ...buyFixture.terms!, limit_price_yen: '900.0000000000000001', quantity: 100, notional_yen: '90000.00000000000001', maximum_entry_price_yen: null },
  remaining_reward: { status: 'insufficient', reason: '合成の観測終値での残存見返りが不足。' },
  saved_sell_limit_comparison: comparison,
}
const render = (value: SavedSellItemView) => renderToStaticMarkup(createElement(ProposalItemCard, { item: value }))

describe('保存売却下限の保有機会費用', () => {
  it('作者下限・元評価株価・終値を別にし、下方見返りを保持する', () => {
    const html = render(item)
    for (const text of ['保存売却下限と保有継続の比較', '22.2222 %', '-22.2222 %', '12 か月', '中心ケース（Base）の保有継続', '下方ケース（Downside）の保有継続']) expect(html).toContain(text)
    expect(html).toMatch(/比較に用いた売却下限<\/dt><dd[^>]*>900\.0000000000000001 円<\/dd>/)
    expect(html).toMatch(/評価に用いた株価<\/dt><dd[^>]*>1000 円<\/dd>/)
    expect(html).toMatch(/保存株価（未調整終値）<\/dt><dd[^>]*>1300\.0000000000000001 円<\/dd>/)
    expect(html).toContain('売却後の運用収益ではありません')
    expect(html).toContain('数値だけで売却や保有へ変更しません')
    expect(html).toContain('投資家税・売買費用・配当再投資は含みません')
    expect(html).toContain('全量売却 · sell')
  })
  it.each(['null', 'absent'] as const)('旧表示 %s は0や保有支持へ補完しない', mode => {
    const value = { ...sellFixture, saved_sell_limit_comparison: null }
    if (mode === 'absent') delete (value as Partial<SavedSellItemView>).saved_sell_limit_comparison
    const html = render(value)
    expect(html).toContain('この表示データには下限での比較がありません')
    expect(html).toContain('保有継続の支持に置き換えていません')
    expect(html).not.toContain('売却下限に対する条件付き年率')
  })
  it('全損ケースの0価値とマイナス年率を欠測へ置き換えない', () => {
    const html = render({ ...item, saved_sell_limit_comparison: { ...comparison, downside: { total_value_per_share_yen: '0', total_return_pct: '-100.0000', annualized_return_pct: '-100.0000' } } })
    expect(html).toContain('-100.0000 %')
    expect(html).toContain('>0 円<')
  })
  it('表示文字列をHTMLとして実行せず末尾まで残す', () => {
    const html = render({ ...item, saved_sell_limit_comparison: { ...comparison, calculation_price_yen: '<img src=x onerror=alert(1)>末尾' } })
    expect(html).toContain('&lt;img src=x onerror=alert(1)&gt;末尾')
    expect(html).not.toContain('<img src=x')
  })
  it('buy/holdへ売却比較を追加しない', () => {
    for (const value of [buyFixture, holdFixture]) expect(renderToStaticMarkup(createElement(ProposalItemCard, { item: value }))).not.toContain('保存売却下限と保有継続の比較')
  })
})
