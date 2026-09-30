import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, it, vi } from 'vitest'

import type { HoldingView, ReviewSetView } from '../src/api/types'
import { TradingViewWatchlistExportButton } from '../src/components/TradingViewWatchlistExportButton'
import { TooltipProvider } from '../src/components/ui/tooltip'
import { tradingViewChartUrl, tradingViewWatchlistText } from '../src/lib/trading-view'
import { HoldingsTable } from '../src/pages/DashboardPage'
import { ReviewSetSection } from '../src/pages/stocks/ReviewSetSection'

describe('TradingView watchlist TXT', () => {
  it('keeps the source order, deduplicates symbols, and preserves alphanumeric tickers', () => {
    expect(tradingViewWatchlistText(['7203', '130A', '7203', '8306'])).toBe('TSE:7203,TSE:130A,TSE:8306')
    expect(tradingViewWatchlistText([])).toBe('')
    expect(tradingViewChartUrl('130A')).toContain('TSE%3A130A')
  })

  it('downloads UTF-8 TXT and releases the object URL', async () => {
    let blob: Blob | undefined
    const link = { href: '', download: '', click: vi.fn(), remove: vi.fn() }
    const appendChild = vi.fn()
    const createObjectURL = vi.spyOn(URL, 'createObjectURL').mockImplementation((value) => {
      blob = value as Blob
      return 'blob:watchlist'
    })
    const revokeObjectURL = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    vi.stubGlobal('document', { createElement: () => link, body: { appendChild } })
    try {
      const button = TradingViewWatchlistExportButton({ tickers: ['7203', '130A'], filename: 'holdings-2026-09-30-tradingview.txt', label: 'TXT出力' })
      button.props.onClick()
      expect(blob?.type).toBe('text/plain;charset=utf-8')
      expect(await blob?.text()).toBe('TSE:7203,TSE:130A')
      expect(link.download).toBe('holdings-2026-09-30-tradingview.txt')
      expect(appendChild).toHaveBeenCalledWith(link)
      expect(link.click).toHaveBeenCalledOnce()
      expect(link.remove).toHaveBeenCalledOnce()
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:watchlist')
      expect(createObjectURL).toHaveBeenCalledOnce()
    } finally {
      vi.unstubAllGlobals()
      vi.restoreAllMocks()
    }
  })

  it('prevents empty downloads', () => {
    const button = TradingViewWatchlistExportButton({ tickers: [], filename: 'empty.txt', label: 'TXT出力' })
    expect(button.props.disabled).toBe(true)
    const createObjectURL = vi.spyOn(URL, 'createObjectURL')
    button.props.onClick()
    expect(createObjectURL).not.toHaveBeenCalled()
    vi.restoreAllMocks()
  })

  it('uses all displayed holdings including unvalued ones', () => {
    const holdings = ['7203', '130A'].map((ticker) => ({
      ticker, company_name: null, sector: '', quantity: 100, deployed_cost_yen: 1000,
      market_price_yen: null, market_price_as_of: null, market_value_yen: null,
      unrealized_pnl_yen: null, unrealized_pnl_pct: null, pmax_raw_yen: null,
      pmax_gap_pct: null, latest_thesis_id: null, disposition: null, next_earnings_date: null,
    }) satisfies HoldingView)
    const html = renderToStaticMarkup(createElement(TooltipProvider, null,
      createElement(MemoryRouter, null, createElement(HoldingsTable, { holdings, warnings: [], ledgerAsOf: '2026-09-30' })),
    ))
    expect(html).toContain('保有銘柄をTradingView用TXTに出力')
    expect(html).toContain('7203')
    expect(html).toContain('130A')
    expect(html).toContain('未評価')
  })

  it('shows the selected Review Set export and disables it when entries are empty', () => {
    const reviewSet = { created_at: '2026-09-30T19:00:00+09:00', entries: [] } as unknown as ReviewSetView
    const html = renderToStaticMarkup(createElement(MemoryRouter, null,
      createElement(ReviewSetSection, { reviewSet, runAsOf: '2026-09-30' }),
    ))
    expect(html).toContain('Review SetをTradingView用TXTに出力')
    expect(html).toContain('disabled')
    const absent = renderToStaticMarkup(createElement(MemoryRouter, null,
      createElement(ReviewSetSection, { reviewSet: null, runAsOf: '2026-09-30' }),
    ))
    expect(absent).not.toContain('TradingView用TXT')
  })
})
