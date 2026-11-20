import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { SavedBuyCapitalComparisonView, SavedBuyItemView } from '../src/api/types'
import { ProposalItemCard } from '../src/pages/ProposalPage'
import { buyFixture, holdFixture, partialBuyFixture, reviewedDeferFixture, sellFixture } from './fixtures/proposals'

// Synthetic conditional arithmetic only, not an actual saved recommendation.
const comparison: SavedBuyCapitalComparisonView = {
  scope: 'available_cash_only', execution_assumption: 'full_fill_at_limit',
  return_basis: 'conditional_pretax_without_reinvestment', investor_taxes_and_fees_included: false,
  cash_basis: 'constant_nominal_without_interest', horizon_months: 12,
  quantity_basis: 'author_selected', quantity: 100, limit_price_yen: '1200.0000',
  notional_yen: '120000.0000', unspent_cash_yen: '180000.0000', cash_only_terminal_value_yen: '300000.0000',
  base: { investment_total_value_yen: '140000.0000', profit_loss_yen: '20000.0000', capital_total_value_yen: '320000.0000' },
  downside: { investment_total_value_yen: '90000.0000', profit_loss_yen: '-30000.0000', capital_total_value_yen: '270000.0000' },
  time_sensitivity: null,
}

function render(item: SavedBuyItemView) {
  return renderToStaticMarkup(createElement(ProposalItemCard, { item }))
}

describe('保存指値・数量の金額比較', () => {
  it('作者の数量・指値と下方損失、残cash・現金維持を別表示する', () => {
    const html = render({ ...buyFixture, saved_buy_comparison: comparison })
    for (const value of ['1200.0000 円', '100 株', '120000.0000 円', '180000.0000 円', '300000.0000 円', '20000.0000 円', '-30000.0000 円', '320000.0000 円', '270000.0000 円']) expect(html).toContain(value)
    for (const label of ['作者が選んだ数量', '現金維持との差', '中心ケース（Base）の金額', '下方ケース（Downside）の金額']) expect(html).toContain(label)
    expect(html).toContain('現在価格・提案指値での再計算ではありません。')
    expect(html).toContain('この銘柄だけが保存指値で全量約定した条件付き比較')
    expect(html).toContain('他の買付・既保有を含むNAVではありません。')
    expect(html).toContain('銘柄間で残cashや終期合計を足し合わせません。')
    expect(html).toContain('投資家税・売買費用は含まず、配当再投資なし・cash利息なし')
    expect(html).toContain('実績・期待値・最大損失・約定保証ではありません。')
    expect(html).not.toContain('数量上限')
  })

  it.each([null, undefined])('旧未収録/nullを0や仮の数量に変えない: %s', value => {
    const item = { ...buyFixture, saved_buy_comparison: value } as SavedBuyItemView
    const html = render(item)
    expect(html).toContain('この表示データには金額比較がありません。')
    expect(html).not.toContain('作者が選んだ数量')
    expect(html).not.toContain('この銘柄への投下額')
    expect(html).not.toContain('下方ケース（Downside）の金額')
  })

  it('具体条件のなかった旧買付案に保存指値・数量の比較を表示しない', () => {
    const html = render(partialBuyFixture)
    expect(html).toContain('作者指定の上限（具体条件未確定）')
    expect(html).not.toContain('保存指値・選択数量の金額比較')
    expect(html).not.toContain('0 株')
    expect(html).not.toContain('下方ケース（Downside）の金額')
  })

  it.each([holdFixture, sellFixture, reviewedDeferFixture])('buy以外へ買付算術を付けない: $action', item => {
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item }))
    expect(html).not.toContain('保存指値・選択数量の金額比較')
  })

  it('金額文字列の精度を落とさずHTMLとして解釈しない', () => {
    const html = render({ ...buyFixture, saved_buy_comparison: { ...comparison, downside: {
      ...comparison.downside, profit_loss_yen: '-123.0000000000000001', capital_total_value_yen: '<img src=x onerror=alert(1)>',
    } } })
    expect(html).toContain('-123.0000000000000001 円')
    expect(html).toContain('&lt;img src=x onerror=alert(1)&gt; 円')
    expect(html).not.toContain('<img src=x')
    expect(html).toContain('break-all')
  })
})
