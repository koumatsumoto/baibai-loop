import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, it, vi } from 'vitest'

import type { HoldingView } from '../src/api/types'
import { TooltipProvider } from '../src/components/ui/tooltip'
import { HoldingsTable } from '../src/pages/DashboardPage'
import { CandidateSection } from '../src/pages/stocks/CandidateSection'
import { stockAnalysisRow, stockRun, candidateMethod } from './helpers/stock-analysis'

const exportsSeen = vi.hoisted(() => [] as { tickers: readonly string[]; filename: string }[])
vi.mock('../src/components/TradingViewWatchlistExportButton', () => ({
  TradingViewWatchlistExportButton: (props: { tickers: readonly string[]; filename: string }) => {
    exportsSeen.push(props)
    return null
  },
}))

describe('watchlist export sources', () => {
  it('passes every displayed holding including those without a market value', () => {
    exportsSeen.length = 0
    const holdings = ['7203', '130A'].map((ticker) => ({
      ticker, company_name: null, sector: '', quantity: 100, deployed_cost_yen: 1000,
      market_price_yen: null, market_price_as_of: null, market_value_yen: null,
      unrealized_pnl_yen: null, unrealized_pnl_pct: null, pmax_raw_yen: null,
      pmax_gap_pct: null, pmax_gap_unavailable_reason: null, latest_assessment_id: null, stance: null, next_earnings_date: null,
    }) satisfies HoldingView)
    renderToStaticMarkup(createElement(TooltipProvider, null,
      createElement(MemoryRouter, null, createElement(HoldingsTable, { holdings, warnings: [], ledgerAsOf: '2026-09-30T23:15:00+09:00' })),
    ))
    expect(exportsSeen).toMatchObject([{ tickers: ['7203', '130A'], filename: 'holdings-2026-09-30-tradingview.txt' }])
  })

  it('exports the same candidate ticker set in canonical order', () => {
    exportsSeen.length = 0
    const rows = ['7203', '130A', '9999'].map(ticker => stockAnalysisRow({ ticker,
      ...(ticker === '9999' ? { candidate: { common_eligible: true, reasons: [], normalized_per_3fy_sector_gap: null } } : {}),
    }))
    renderToStaticMarkup(createElement(MemoryRouter, null,
      createElement(CandidateSection, { rows, run: stockRun, method: candidateMethod }),
    ))
    expect(exportsSeen).toMatchObject([{ tickers: ['130A', '7203'], filename: 'candidates-2026-09-30-tradingview.txt' }])
  })
})
