import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'

import { fetchJson } from '../src/api/client'
import type { DashboardView } from '../src/api/types'
import { TooltipProvider } from '../src/components/ui/tooltip'
import { DashboardPage } from '../src/pages/DashboardPage'

const hooks = vi.hoisted(() => ({
  values: new Map<number, unknown>(),
  setters: [] as ReturnType<typeof vi.fn>[],
  effects: [] as (() => void)[],
}))
vi.mock('react', async original => ({
  ...await original<typeof import('react')>(),
  useState: (initial: unknown) => {
    const index = hooks.setters.length
    const setter = vi.fn()
    hooks.setters.push(setter)
    return [hooks.values.has(index) ? hooks.values.get(index) : typeof initial === 'function' ? initial() : initial, setter]
  },
  useEffect: (setup: () => void) => { hooks.effects.push(setup) },
}))
vi.mock('../src/api/client', () => ({ fetchJson: vi.fn() }))

function dashboard(): DashboardView {
  return {
    generated_at: '2026-10-16T09:00:00+09:00', ledger_exists: true,
    ledger_error: null, ledger_as_of: null, valuation_error: null,
    valuation_as_of: null,
    total_capital_yen: null, available_cash_yen: null, reserved_cash_yen: null,
    holdings_market_value_yen: null, deployed_cost_yen: null,
    realized_gross_pnl_yen: null, cash_pct: null, reserved_pct: null, deployed_pct: null,
    holdings: [], reservations: [], warnings: [],
  }
}
function render(): string {
  const page = DashboardPage()
  return renderToStaticMarkup(createElement(TooltipProvider, null,
    createElement(MemoryRouter, null, page),
  ))
}
afterEach(() => {
  vi.resetAllMocks()
  hooks.values.clear()
  hooks.setters = []
  hooks.effects = []
})

it.each(['invalid ledger', ''])('does not describe an unreadable ledger as an empty portfolio (%s)', ledgerError => {
  hooks.values.set(0, { ...dashboard(), ledger_error: ledgerError })
  const html = render()
  expect(html).toContain('資産・保有・未約定注文を確認できません')
  expect(html).toContain(ledgerError)
  expect(html).not.toContain('保有銘柄はありません')
  expect(html).not.toContain('銘柄を保有')
  expect(html).not.toContain('資産と損益')
  expect(html).not.toContain('前回実行からの変化')
})

it.each(['obsolete market SQLite schema', 'file is not a database'])(
  'keeps ledger facts visible and labels only market valuation as unreadable (%s)', valuationError => {
    const data: DashboardView = {
      ...dashboard(), valuation_error: valuationError,
      available_cash_yen: 10_190_500, reserved_cash_yen: 119_000,
      deployed_cost_yen: 99_000, realized_gross_pnl_yen: 6_000,
      holdings: [{
        ticker: '2331', company_name: null, sector: 'サービス業', quantity: 100,
        deployed_cost_yen: 99_000, market_price_yen: null, market_price_as_of: null,
        market_value_yen: null, unrealized_pnl_yen: null, unrealized_pnl_pct: null,
      }],
      reservations: [{
        reservation_id: 'reservation-8929-pending', side: 'buy', ticker: '8929', sector: '不動産業',
        remaining_quantity: 100, price_guard_yen: '1190', reserved_yen: 119_000,
        expires_at: '2026-07-31T15:30:00+09:00', expiry_label: '2026-07-31', route_label: null,
      }],
    }
    hooks.values.set(0, data)
    const html = render()
    expect(html).toContain('市場評価を読み込めません')
    expect(html).toContain(valuationError)
    expect(html).toContain('資産と損益')
    expect(html).toContain('￥10,190,500')
    expect(html).toContain('￥119,000')
    expect(html).toContain('+￥6,000')
    expect(html).toContain('2331')
    expect(html).toContain('8929')
    expect(html).toContain('100 株')
    expect(html).toContain('￥990')
    expect(html).toContain('未評価: 2331')
    expect(html).not.toContain('台帳を読み込めません')
    expect(html).not.toContain('保有銘柄はありません')
    expect(html).not.toContain('株価基準')
  },
)

it('keeps portfolio information visible when the independent outcomes request fails', () => {
  hooks.values.set(0, dashboard())
  hooks.values.set(2, 'outcomes unavailable')
  const html = render()
  expect(html).toContain('資産と損益')
  expect(html).toContain('保有銘柄はありません')
  expect(html).toContain('運用成績を読み込めません')
  expect(html).not.toContain('Dashboard read error')
})

it('keeps the initial loading state while both requests are pending', () => {
  const html = render()
  expect(html).toContain('資産状況を読み込んでいます')
  expect(html).not.toContain('資産と損益')
  expect(html).not.toContain('運用成績はまだありません')
})

it('keeps portfolio information visible while outcomes are pending', () => {
  hooks.values.set(0, dashboard())
  const html = render()
  expect(html).toContain('資産と損益')
  expect(html).toContain('保有銘柄はありません')
  expect(html).not.toContain('運用成績はまだありません')
})

it.each([{ outcomes: [] }, { outcomes: [{
  outcome_id: 'retained-outcome', horizon: '1y', period_start_date: '2025-01-01',
  period_end_date: '2025-12-31', status: 'resolved', reason: null,
  portfolio_twr_pct: 10.2, benchmark_cumulative_return_pct: 8.2,
}] }])('keeps readable outcomes visible while the portfolio request is pending (%j)', ({ outcomes }) => {
  hooks.values.set(1, { error: null, outcomes })
  const html = render()
  expect(html).toContain(outcomes.length ? 'retained-outcome' : '運用成績はまだありません')
  expect(html).toMatch(/role="status"[^>]*>資産状況を読み込んでいます/)
  expect(html).not.toContain('資産と損益')
  expect(html).not.toContain('保有銘柄はありません')
  expect(html).not.toContain('表示データ生成')
})

it.each(['request', 'saved-view'])('shows an outcomes %s error while the portfolio request is pending', failure => {
  if (failure === 'request') hooks.values.set(2, 'outcomes unavailable')
  else hooks.values.set(1, { error: 'outcomes unavailable', outcomes: [] })
  const html = render()
  expect(html).toContain('運用成績を読み込めません')
  expect(html).toContain('outcomes unavailable')
  expect(html).toMatch(/role="status"[^>]*>資産状況を読み込んでいます/)
  expect(html).not.toContain('資産と損益')
  expect(html).not.toContain('運用成績はまだありません')
})

it.each([{ outcomes: [] }, { outcomes: [{
  outcome_id: 'retained-outcome', horizon: '1y', period_start_date: '2025-01-01',
  period_end_date: '2025-12-31', status: 'resolved', reason: null,
  portfolio_twr_pct: 10.2, benchmark_cumulative_return_pct: 8.2,
}] }])('keeps readable outcomes visible when the portfolio request fails (%j)', ({ outcomes }) => {
  hooks.values.set(1, { error: null, outcomes })
  hooks.values.set(3, 'API request failed: 503 Service Unavailable')
  const html = render()
  expect(html).toContain('資産・保有・未約定注文を読み込めません')
  expect(html).toContain('API request failed: 503 Service Unavailable')
  expect(html).toContain(outcomes.length ? 'retained-outcome' : '運用成績はまだありません')
  expect(html).not.toContain('資産と損益')
  expect(html).not.toContain('保有銘柄はありません')
  expect(html).not.toContain('表示データ生成')
})

it('shows both independent request errors without inventing empty data', () => {
  hooks.values.set(2, 'outcomes unavailable')
  hooks.values.set(3, 'portfolio unavailable')
  const html = render()
  expect(html).toContain('portfolio unavailable')
  expect(html).toContain('outcomes unavailable')
  expect(html).not.toContain('運用成績はまだありません')
  expect(html).not.toContain('保有銘柄はありません')
})

it('keeps an unreadable outcome view distinct when the portfolio request also fails', () => {
  hooks.values.set(1, { error: 'saved outcomes unavailable', outcomes: [] })
  hooks.values.set(3, 'portfolio unavailable')
  const html = render()
  expect(html).toContain('portfolio unavailable')
  expect(html).toContain('saved outcomes unavailable')
  expect(html).not.toContain('運用成績はまだありません')
})

it('routes a portfolio request failure to its own state, not the outcomes error', async () => {
  const outcomes = { error: null, outcomes: [] }
  vi.mocked(fetchJson)
    .mockRejectedValueOnce(new Error('portfolio unavailable'))
    .mockResolvedValueOnce(outcomes)
  DashboardPage()
  hooks.effects[0]()
  await Promise.resolve()
  await Promise.resolve()
  expect(hooks.setters[3]).toHaveBeenCalledWith('portfolio unavailable')
  expect(hooks.setters[2]).not.toHaveBeenCalled()
  expect(hooks.setters[1]).toHaveBeenCalledWith(outcomes)
})

it('routes an outcomes request failure to its own state, not the portfolio error', async () => {
  vi.mocked(fetchJson)
    .mockResolvedValueOnce(dashboard())
    .mockRejectedValueOnce(new Error('outcomes unavailable'))
  DashboardPage()
  hooks.effects[0]()
  await Promise.resolve()
  await Promise.resolve()
  expect(hooks.setters[2]).toHaveBeenCalledWith('outcomes unavailable')
  expect(hooks.setters[3]).not.toHaveBeenCalled()
  expect(hooks.setters[0]).toHaveBeenCalledWith(dashboard())
})


it('distinguishes display generation, last ledger record, and quote basis', () => {
  hooks.values.set(0, { ...dashboard(),
    generated_at: '2026-10-16T13:43:00Z',
    ledger_as_of: '2026-10-12T01:30:00Z',
    valuation_as_of: '2026-10-15',
  })
  const html = render()
  expect(html).toMatch(/表示データ生成 <time[^>]*dateTime="2026-10-16T13:43:00Z"[^>]*>2026-10-16 22:43 JST/)
  expect(html).toMatch(/台帳最終記録 <time[^>]*dateTime="2026-10-12T01:30:00Z"[^>]*>2026-10-12 10:30 JST/)
  expect(html).toContain('株価基準 2026-10-15')
  expect(html).toContain('表示の基準日の保存終値に基づきます')
  expect(html).toContain('リアルタイム価格ではありません')
})

it('still shows the generation stamp when the ledger is absent or unreadable', () => {
  for (const ledgerError of [null, 'invalid ledger']) {
    hooks.values.set(0, { ...dashboard(), ledger_exists: false, ledger_error: ledgerError })
    hooks.setters = []
    const html = render()
    expect(html).toContain('表示データ生成')
    expect(html).not.toContain('台帳最終記録')
  }
})

it('does not describe an unavailable saved outcome view as no outcomes', () => {
  hooks.values.set(0, dashboard())
  hooks.values.set(1, { error: '保存済み運用成績を読み取れません。', outcomes: [] })
  const html = render()
  expect(html).toContain('資産と損益')
  expect(html).toContain('運用成績を読み込めません')
  expect(html).toContain('保存済み運用成績を読み取れません。')
  expect(html).not.toContain('運用成績はまだありません')
})

it('describes a readable empty outcome store as no outcomes', () => {
  hooks.values.set(0, dashboard())
  hooks.values.set(1, { error: null, outcomes: [] })
  const html = render()
  expect(html).toContain('運用成績はまだありません')
  expect(html).not.toContain('運用成績を読み込めません')
})


it.each([undefined, null])('keeps readable legacy and current outcome views visible (error=%s)', error => {
  hooks.values.set(0, dashboard())
  hooks.values.set(1, {
    ...(error === undefined ? {} : { error }),
    outcomes: [{
      outcome_id: 'synthetic-outcome', horizon: '1y', period_start_date: '2024-12-31',
      period_end_date: '2025-12-31', status: 'resolved', reason: null,
      portfolio_twr_pct: 10.2, benchmark_cumulative_return_pct: 8.2,
    }],
  })
  const html = render()
  expect(html).toContain('synthetic-outcome')
  expect(html).toContain('10.2%')
  expect(html).not.toContain('運用成績を読み込めません')
})

it('preserves the empty state of a readable legacy outcome view', () => {
  hooks.values.set(0, dashboard())
  hooks.values.set(1, { outcomes: [] })
  const html = render()
  expect(html).toContain('運用成績はまだありません')
  expect(html).not.toContain('運用成績を読み込めません')
})
