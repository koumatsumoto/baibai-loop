import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import type { MarketSnapshotView, SecurityMarketContextView } from '../src/api/types'
import { MarketSnapshotSection, SecurityMarketContextSection } from '../src/pages/research/MarketEvidence'

function render(node: ReturnType<typeof createElement>) { return renderToStaticMarkup(createElement(MemoryRouter, null, node)) }
describe('市場根拠の表示', () => {
  it('要求日・観測日・peer端点と、騰落率・差の単位を分ける', () => {
    const context: SecurityMarketContextView = { as_of: '2026-10-13', return_basis: 'price_return_excluding_cash_dividends', master: null, benchmark_trend: null,
      price: { resolved_date: '2026-10-09', close: 100, price_basis: 'split_adjusted_close', return_1d: null, return_5d: null, return_20d: 0.02, return_60d: null, high_52w: 120, low_52w: 80, gap_from_52w_low: 0.25, gap_from_52w_high: null, self_range_percentile: 0.6, self_range_window_bars: 100, realized_vol_20d: null, avg_turnover_20d_oku: null, bar_count: 100 },
      relative: { benchmark_ticker: '1321', relative_1d: null, relative_5d: null, relative_20d: 0.001, relative_60d: null, sector: { sector_33: '合成業種', peer_count: 3, peer_median_return_20d: 0.01, relative_return_20d: 0.01, comparison_start_date: '2026-09-11', comparison_end_date: '2026-10-09', master_snapshot_date: '2026-10-08' } },
    }
    const html = render(createElement(SecurityMarketContextSection, { context }))
    for (const text of ['要求日 2026-10-13', '観測日 2026-10-09', '2.0%', '+0.1pt', '2026-09-11 → 2026-10-09', '対象銘柄を除いた 3銘柄', '配当を含みません', 'Pmax・注文の未調整価格とは基準が異なります', '未算出']) expect(html).toContain(text)
  })
  it('指数の欠測を0の線にせず、観測されたゼロと業種探索を残す', () => {
    const snapshot: MarketSnapshotView = { as_of: '2026-10-13', benchmark_ticker: '1321', history_weeks: 1, master_snapshot_date: null, return_basis: 'price_return_excluding_cash_dividends',
      points: [{ date: '2026-10-13', observation_date: '2026-10-09', benchmark_return_20d: null, benchmark_return_60d: null, breadth_pct_above_ma20: 0, breadth_sample_size: 100, benchmark_trend: 'unknown' }],
      sectors: [{ sector_33: '合成業種', ticker_count: 3, median_return_20d: 0, median_return_60d: null, pct_above_ma20: 0 }],
    }
    const html = render(createElement(MarketSnapshotSection, { snapshot }))
    for (const text of ['指数騰落の推移は未算出', '0.0%', '未算出', '/stocks/explore?sector=', '母集団が異なります', '配当を含まない']) {
      expect(html).toContain(text)
    }
    expect(html).not.toContain('<svg')
    expect(render(createElement(SecurityMarketContextSection, { context: null }))).toContain('保存済みの価格・比較情報はありません')
  })
})
