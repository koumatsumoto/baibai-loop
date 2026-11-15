import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { SavedAssessmentSourceView, SavedResolvedValuationView } from '../src/api/types'
import { ProposalItemCard } from '../src/pages/ProposalPage'
import { buyFixture } from './fixtures/proposals'

const source = (id: string): SavedAssessmentSourceView => ({
  source_id: id, source_tier: 'primary', ref: `https://example.com/${id}.pdf`,
  provider: null, dataset: null, as_of: '2026-10-10', retrieved_at: '2026-10-11T12:34:56+09:00',
  used_for: `${id}の資料で仮定を検討。<script>ではない。`,
})
const valuation: SavedResolvedValuationView = {
  status: 'resolved', price_yen: '1000', price_as_of: '2026-10-26', price_observed_at: null,
  price_basis: 'last_close_unadjusted', horizon_months: 24, required_annual_return_pct: '8',
  base: { terminal_value_per_share_yen: '1500', cash_distribution_per_share_yen: '20', annualized_return_pct: '23.2883', calculation: '合成Baseの将来仮定。', source_ids: ['base-only'], sources: [source('base-only')] },
  downside: { terminal_value_per_share_yen: '600', cash_distribution_per_share_yen: '10', annualized_return_pct: '-21.8975', calculation: '合成Downsideの将来仮定。', source_ids: ['downside-only'], sources: [source('downside-only')] },
}
function render(value: SavedResolvedValuationView) {
  return renderToStaticMarkup(createElement(ProposalItemCard, { item: {
    ...buyFixture, assessment: { ...buyFixture.assessment!, valuation: value },
  } }))
}

describe('価値ケースごとの保存引用', () => {
  it('Base/Downsideの対応と資料時計を別々に残し、将来値の証明にしない', () => {
    const html = render(valuation)
    const base = html.split('中心ケース（Base）の保存前提')[1].split('下方ケース（Downside）の保存前提')[0]
    const downside = html.split('下方ケース（Downside）の保存前提')[1]
    expect(base).toContain('https://example.com/base-only.pdf')
    expect(base).not.toContain('downside-only')
    expect(downside).toContain('https://example.com/downside-only.pdf')
    expect(downside).not.toContain('base-only')
    for (const text of ['根拠の対象日', '2026-10-10', '取得時刻', '2026-10-11T12:34:56+09:00', '作者が引用した保存資料', '将来値の確定・予測の的中や独立した支持票を意味しません']) expect(html).toContain(text)
    expect(html).toContain('&lt;script&gt;')
    expect(html).not.toContain('<script>')
  })

  it.each([null, undefined])('旧表示の引用未収録を埋めず明示する: %s', absent => {
    const html = render({ ...valuation, base: { ...valuation.base, source_ids: absent, sources: absent } } as SavedResolvedValuationView)
    const base = html.split('中心ケース（Base）の保存前提')[1].split('下方ケース（Downside）の保存前提')[0]
    expect(base).toContain('このケースの引用対応が含まれていません。根拠がないことを示しません。')
    expect(base).not.toContain('base-only')
    expect(base).not.toContain('downside-only')
    expect(html).toContain('https://example.com/downside-only.pdf')
  })

  it('参照情報が無ければ別ケースから補完せず既存の欠測表示を使う', () => {
    const html = render({ ...valuation, base: { ...valuation.base, sources: [] } })
    const base = html.split('中心ケース（Base）の保存前提')[1].split('下方ケース（Downside）の保存前提')[0]
    expect(base).toContain('base-only')
    expect(base).toContain('参照先の表示情報なし')
    expect(base).not.toContain('https://example.com/')
  })
})
