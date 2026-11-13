import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { SavedResolvedValuationView } from '../src/api/types'
import { ProposalItemCard } from '../src/pages/ProposalPage'
import { buyFixture, holdFixture, reviewedDeferFixture } from './fixtures/proposals'

const value: SavedResolvedValuationView = {
  status: 'resolved',
  price_yen: '1000.0000000000000001',
  price_as_of: '2026-10-23',
  price_observed_at: '2026-10-23T15:30:00+09:00',
  price_basis: 'last_close_unadjusted',
  horizon_months: 24,
  required_annual_return_pct: '9.5',
  base: { terminal_value_per_share_yen: '1210.0000000000000001', cash_distribution_per_share_yen: '0', annualized_return_pct: '10.0000', calculation: '合成中心：利益と倍率を固定。\n追加の成長は見込まない。' },
  downside: { terminal_value_per_share_yen: '810', cash_distribution_per_share_yen: '0', annualized_return_pct: '-10.0000', calculation: '合成下方：需要減と採算悪化。' },
}

function withValue() {
  const item = structuredClone(buyFixture)
  item.assessment!.valuation = value
  return item
}

describe('保存評価の見返り', () => {
  it('評価の価格・時計・期間・要求率と中心/下方の前提を示し、提案価格と区別する', () => {
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item: withValue() }))
    for (const text of ['保存評価の見返り', '評価に用いた株価', '1000.0000000000000001 円', '24 か月', '9.5 %', '中心ケース（Base）', '下方ケース（Downside）', '10.00%', '-10.00%', '総受取額の年率換算', '1210.0000000000000001 円', value.base.calculation, value.downside.calculation]) expect(html).toContain(text)
    expect(html).toContain('dateTime="2026-10-23T15:30:00+09:00"')
    expect(html).toContain('dateTime="2026-10-23"')
    expect(html).toContain('dateTime="2026-10-26"')
    expect(html).toContain('の保存評価です。計算は評価に用いた価格と仮定を固定しています。')
    expect(html).toContain('現在価格・提案指値での再計算ではありません。')
    expect(html).toContain('投資家税・費用前、配当再投資なし。期待値・勝率ではなく、下方ケースも最大損失を示しません。')
    expect(html).toContain('1300.0000000000000001 円')
    expect(html).toContain('1200.0000 円')
    expect(html.indexOf('保存評価の見返り')).toBeLessThan(html.indexOf('7モデルの観点'))
    expect(html.match(/buyの反対論：利益率改善が一過性の可能性。/g)).toHaveLength(1)
    expect(html).toContain('仮定・計算と最も強い反対論')
  })

  it.each(['missing', 'null'] as const)('旧表示データの未記録をゼロや未確定へ変えない: %s', kind => {
    const item = withValue()
    if (kind === 'missing') Reflect.deleteProperty(item.assessment!, 'valuation')
    else item.assessment!.valuation = null
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item }))
    expect(html).toContain('この表示データには評価の見返りが含まれていません。')
    expect(html).not.toContain('価値評価は未確定')
    expect(html).not.toContain('総受取額の年率換算')
    expect(html).not.toContain('0.00%')
    expect(html).toContain(item.assessment!.review.strongest_countercase)
  })

  it('評価未確定の理由を表示し、保留を経済的見送りに変えない', () => {
    const item = structuredClone(reviewedDeferFixture)
    item.assessment!.valuation = { status: 'unresolved', reason: '回収時期を確定できない。' }
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item }))
    expect(html).toContain('価値評価は未確定')
    expect(html).toContain('回収時期を確定できない。')
    expect(html).toContain('判断保留 · defer')
    expect(html).not.toContain('この表示データには評価の見返りが含まれていません。')
    expect(html).not.toContain('総受取額の年率換算')
  })

  it('保有の見返りは今回の評価だけを使い、当初の反対論も保持する', () => {
    const item = structuredClone(holdFixture)
    item.assessment!.valuation = value
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item }))
    expect(html.match(/総受取額の年率換算/g)).toHaveLength(2)
    expect(html).toContain('current-bの反対論')
    expect(html).toContain('original-aの反対論')
    expect(html).toContain(holdFixture.return_calculation_unavailable_reason)
  })

  it('ゼロ分配・全損を欠測にせず、保存前提をHTMLとして解釈しない', () => {
    const item = withValue()
    item.assessment!.valuation = { ...value, price_observed_at: null, downside: { ...value.downside, terminal_value_per_share_yen: '0', annualized_return_pct: '-100.0000', calculation: '<script>bad()</script>\n保存された条件。' } }
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item }))
    expect(html).toContain('-100.00%')
    expect(html).toContain('0 円')
    expect(html).toContain('&lt;script&gt;bad()&lt;/script&gt;\n保存された条件。')
    expect(html).not.toContain('<script>')
    expect(html).not.toContain('株価の観測時刻')
  })
})
