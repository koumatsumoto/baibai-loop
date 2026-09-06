import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'

import type { DailyDeltaView } from '../src/api/types'
import { DailyDeltaCard } from '../src/pages/DashboardPage'

function delta(total: number): DailyDeltaView {
  return {
    generated_at: '2026-09-04T18:00:00+09:00',
    as_of: '2026-09-04', previous_as_of: '2026-09-03', method_changed: false,
    entered: [], exited: [], holdings: [], unavailable: [],
    holdings_without_fair_value: 0, holdings_without_price: 0,
    er_moves_total: total,
    er_moves: Array.from({ length: Math.min(total, 5) }, (_, index) => ({
      ticker: `${1000 + index}`, company_name: null, er_annual_pct: 20,
      previous_er_annual_pct: 10, change_pp: 10,
    })),
  }
}

function render(view: DailyDeltaView) {
  return renderToStaticMarkup(createElement(MemoryRouter, null,
    createElement(DailyDeltaCard, { delta: view, failed: false })))
}

describe('Daily Delta display', () => {
  it.each([0, 5, 20])('badge counts all %i movers, not just displayed rows', (total) => {
    const html = render(delta(total))
    expect(html).toContain(`>${total} 件</span>`)
    if (total > 5) {
      expect(html).toContain('閾値を超えた変化は 20 件で、上位 5 件を表示。')
    }
  })

  it('does not describe an unmeasured estimate section as no changes', () => {
    const html = render({ ...delta(0), unavailable: ['review_set_estimate'] })
    expect(html).toContain('見積り欠損またはモデルを比較できない')
    expect(html).not.toContain('閾値に触れる変化はありません')
  })

  it('shows method change without an empty-change claim', () => {
    const html = render({ ...delta(0), method_changed: true })
    expect(html).toContain('手法変更')
    expect(html).not.toContain('閾値に触れる変化はありません')
  })
})
