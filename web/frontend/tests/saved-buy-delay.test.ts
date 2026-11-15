import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { SavedBuyCapitalComparisonView } from '../src/api/types'
import { ProposalItemCard } from '../src/pages/ProposalPage'
import { buyFixture, holdFixture, partialBuyFixture, reviewedDeferFixture } from './fixtures/proposals'

const comparison: SavedBuyCapitalComparisonView = {
  scope: 'available_cash_only', execution_assumption: 'full_fill_at_limit',
  return_basis: 'conditional_pretax_without_reinvestment', investor_taxes_and_fees_included: false,
  cash_basis: 'constant_nominal_without_interest', horizon_months: 12,
  quantity_basis: 'author_selected', quantity: 100, limit_price_yen: '1000',
  notional_yen: '100000', unspent_cash_yen: '200000', cash_only_terminal_value_yen: '300000',
  base: { investment_total_value_yen: '110000', profit_loss_yen: '10000', capital_total_value_yen: '310000' },
  downside: { investment_total_value_yen: '70000', profit_loss_yen: '-30000', capital_total_value_yen: '270000' },
  time_sensitivity: {
    assumption: 'terminal_and_cumulative_cash_unchanged', additional_months: 12, horizon_months: 24,
    base: { annualized_return_pct: '10.0000', delayed_annualized_return_pct: '4.8809' },
    downside: { annualized_return_pct: '-30.0000', delayed_annualized_return_pct: '-16.3340' },
  },
}

function render(value = comparison) {
  return renderToStaticMarkup(createElement(ProposalItemCard, { item: { ...buyFixture, saved_buy_comparison: value } }))
}

describe('指値の年率と固定価値の遅延感応度', () => {
  it('元評価価格と指値を分け、同じ損失額の時間リスクを示す', () => {
    const html = render()
    for (const text of ['10.0000 %', '4.8809 %', '-30.0000 %', '-16.3340 %', '-30000 円']) expect(html).toContain(text)
    expect(html).toContain('保存指値での年率と遅延感応度')
    expect(html).toContain('保存指値での条件付き年率')
    expect(html).toContain('12か月遅延・総受取額不変の年率')
    expect(html).toContain('比較期間だけを 12 か月から 24 か月へ延ばす条件例')
    expect(html).toContain('元評価株価の年率や残cashを含む元手全体の年率ではありません。')
    expect(html).toContain('遅延予測・保有期限・新しい評価期間の採択ではありません。')
    expect(html).toContain('損失年率が0へ近づいても上の損失額は減りません。')
    expect(html).toContain('遅延による事業価値・費用の変化は仮定していません。')
    expect(html).toContain('新たな買付推奨を示しません。')
  })

  it.each([null, undefined])('旧欠欄は損益額を保ち、感応度0や安全にしない: %s', value => {
    const html = render({ ...comparison, time_sensitivity: value } as SavedBuyCapitalComparisonView)
    expect(html).toContain('時間感応度が未収録')
    expect(html).toContain('-30000 円')
    expect(html).not.toContain('4.8809 %')
    expect(html).not.toContain('0 %')
  })

  it.each([holdFixture, reviewedDeferFixture, partialBuyFixture])('対象外/条件不足へ時間算術を足さない: $action', item => {
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item }))
    expect(html).not.toContain('保存指値での年率と遅延感応度')
    expect(html).not.toContain('4.8809 %')
  })

  it('小さい負値と文字列を丸め直さずHTMLをescapeする', () => {
    const html = render({ ...comparison, time_sensitivity: { ...comparison.time_sensitivity!, downside: {
      annualized_return_pct: '-0.0001', delayed_annualized_return_pct: '<script>not-rate</script>',
    } } })
    expect(html).toContain('-0.0001 %')
    expect(html).toContain('&lt;script&gt;not-rate&lt;/script&gt; %')
    expect(html).not.toContain('<script>not-rate')
  })
})
