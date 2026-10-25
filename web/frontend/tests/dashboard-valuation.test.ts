import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'

import type { DashboardView, HoldingView, ReservationView } from '../src/api/types'
import { TooltipProvider } from '../src/components/ui/tooltip'
import { HoldingsTable, OutcomeCard, PendingOrdersCard, PortfolioAllocationCard } from '../src/pages/DashboardPage'

function holding(ticker: string): HoldingView {
  return {
    ticker, company_name: null, sector: 'test', quantity: 100,
    deployed_cost_yen: 100_000, market_price_yen: '1100',
    market_price_as_of: '2026-09-24', market_value_yen: 110_000,
    unrealized_pnl_yen: 10_000, unrealized_pnl_pct: 10,
  }
}

function dashboard(overrides: Partial<DashboardView> = {}): DashboardView {
  return {
    generated_at: '2026-09-24T20:00:00+09:00', ledger_exists: true,
    ledger_error: null, ledger_as_of: '2026-09-24', ledger_stale: false,
    valuation_as_of: '2026-09-24', valuation_stale: false,
    total_capital_yen: 210_000, holdings_market_value_yen: 110_000,
    available_cash_yen: 100_000, reserved_cash_yen: 0,
    realized_gross_pnl_yen: 136_100,
    deployed_cost_yen: 100_000, cash_pct: 47.62, reserved_pct: 0,
    deployed_pct: 52.38, holdings: [holding('8255')],
    reservations: [], warnings: [],
    ...overrides,
  }
}

function render(data: DashboardView): string {
  return renderToStaticMarkup(createElement(PortfolioAllocationCard, { data }))
}

describe('Dashboard valuation display', () => {
  it('labels ledger cash and returns without claiming settlement or unreported costs', () => {
    const allocation = render(dashboard())
    const outcomes = renderToStaticMarkup(createElement(OutcomeCard, { outcomes: [] }))
    expect(allocation).toContain('台帳の未予約現金')
    expect(allocation).toContain('受渡未照合')
    expect(allocation).not.toContain('購入余力')
    expect(outcomes).toContain('記録済みの税・費用を反映')
    expect(outcomes).toContain('未報告分は含みません')
  })

  it('shows pending buy and sell orders without treating sell proceeds as reserved cash', () => {
    const reservations: ReservationView[] = [
      { reservation_id: 'buy', side: 'buy', ticker: '1111', sector: '情報・通信業', remaining_quantity: 200, price_guard_yen: '1000', reserved_yen: 200000, expires_at: '2026-10-16T15:30:00+09:00', expiry_label: '今週中', route_label: '取引所（自動振分）' },
      { reservation_id: 'sell', side: 'sell', ticker: '2222', sector: '情報・通信業', remaining_quantity: 100, price_guard_yen: '900', reserved_yen: 0, expires_at: '2026-10-16T15:30:00+09:00', expiry_label: '今週中', route_label: '取引所（自動振分）' },
    ]
    const html = renderToStaticMarkup(createElement(TooltipProvider, null,
      createElement(MemoryRouter, null, createElement(PendingOrdersCard, { reservations })),
    ))
    expect(html).toContain('未約定注文')
    expect(html).toMatch(/買[\s\S]*1111/)
    expect(html).toMatch(/売[\s\S]*2222/)
    expect(html).toContain('￥200,000')
    expect(html).not.toContain('￥90,000')
    expect(html.match(/今週中/g)).toHaveLength(2)
    expect(html.match(/取引所（自動振分）/g)).toHaveLength(2)
  })

  it('shows complete assets, unrealized P&L, allocation, and the market basis date', () => {
    const html = render(dashboard())
    expect(html).toContain('株価基準 2026-09-24')
    expect(html).toContain('￥210,000')
    expect(html).toContain('￥110,000')
    expect(html).toContain('+￥10,000')
    expect(html).toMatch(/評価損益[\s\S]*累積確定損益[\s\S]*\+￥136,100/)
    expect(html).toContain('台帳開始以来の売却損益・税／手数料控除前')
    expect(html).not.toContain('配分データなし')
    expect(html).not.toContain('未評価')
  })

  it('names one unvalued holding without a cash-only chart or partial total', () => {
    const valued = Array.from({ length: 8 }, (_, index) => holding(`${1000 + index}`))
    const unvalued = {
      ...holding('8255'), market_price_yen: null, market_price_as_of: null,
      market_value_yen: null, unrealized_pnl_yen: null, unrealized_pnl_pct: null,
    }
    const html = render(dashboard({
      holdings: [...valued, unvalued], valuation_as_of: null,
      total_capital_yen: null, holdings_market_value_yen: null,
      available_cash_yen: 3_507_000, reserved_cash_yen: 0,
      cash_pct: null, deployed_pct: null,
    }))
    expect(html).toContain('未評価: 8255')
    expect(html).toContain('総資産・評価損益・資産配分を表示できません')
    expect(html).toContain('配分データなし')
    expect(html).toContain('￥3,507,000')
    expect(html).toContain('+￥136,100')
    expect(html).toContain('￥0')
    expect(html).not.toContain('￥880,000')
    expect(html).not.toContain('株価基準 2026-09-24')
  })

  it('keeps zero cash or reservation as complete data', () => {
    for (const [available, reserved] of [[0, 100_000], [100_000, 0]]) {
      const html = render(dashboard({ available_cash_yen: available, reserved_cash_yen: reserved }))
      expect(html).not.toContain('配分データなし')
      expect(html).toContain('￥0')
    }
  })

  it('requires the total as well as every allocation part', () => {
    const html = render(dashboard({ total_capital_yen: null }))
    expect(html).toContain('配分データなし')
    expect(html).not.toContain('￥210,000')
  })

  it('shows a cash-only account with zero equity value', () => {
    const html = render(dashboard({
      holdings: [], holdings_market_value_yen: 0, deployed_cost_yen: 0,
      valuation_as_of: null, total_capital_yen: 100_000,
      deployed_pct: 0, cash_pct: 100,
    }))
    expect(html).toContain('￥100,000')
    expect(html).toContain('+￥136,100')
    expect(html).not.toContain('配分データなし')
    expect(html).not.toContain('未評価')
  })

  it('shows negative, zero, null, and temporarily absent realized amounts', () => {
    for (const [value, expected] of [[-3_000, '-￥3,000'], [0, '￥0'], [null, '—'], [undefined, '—']] as const) {
      const html = render(dashboard({ realized_gross_pnl_yen: value } as DashboardView))
      expect(html).toContain('累積確定損益')
      expect(html).toContain(expected)
    }
  })

  it('keeps each holding and its own price date when valuation bases differ', () => {
    const html = renderToStaticMarkup(createElement(TooltipProvider, null,
      createElement(MemoryRouter, null, createElement(HoldingsTable, {
        holdings: [
          holding('1234'),
          { ...holding('8255'), market_price_yen: null, market_price_as_of: null,
            market_value_yen: null, unrealized_pnl_yen: null, unrealized_pnl_pct: null },
        ],
        warnings: [],
      })),
    ))
    expect(html).toContain('株価基準')
    expect(html).toContain('2026-09-24')
    expect(html).toContain('8255')
    expect(html).toContain('未評価')
    expect(html).toContain('取得')
    expect(html).toContain('￥1,000')
    expect(html).toMatch(/評価損益 合計<\/span><span[^>]*>未評価/)
  })

})
