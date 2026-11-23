import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

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
    holding_quantity_unresolved: false, valuation_as_of: null,
    total_capital_yen: null, available_cash_yen: null, reserved_cash_yen: null,
    holdings_market_value_yen: null, deployed_cost_yen: null,
    realized_gross_pnl_yen: null, cash_pct: null, reserved_pct: null, deployed_pct: null,
    holdings: [], reservations: [], warnings: [],
  }
}
const pendingBuy: DashboardView['reservations'][number] = {
  reservation_id: 'reservation-8929-pending', side: 'buy', ticker: '8929', sector: '不動産業',
  remaining_quantity: 100, price_guard_yen: '1190', reserved_yen: 119_000,
  expires_at: '2026-07-31T15:30:00+09:00', expiry_label: '2026-07-31', route_label: null,
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
  vi.unstubAllGlobals()
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
      reservations: [pendingBuy],
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
    expect(html).toContain('1 銘柄を保有')
    expect(html).not.toContain('保有株数は未照合')
    expect(html).not.toContain('台帳を読み込めません')
    expect(html).not.toContain('保有銘柄はありません')
    expect(html).not.toContain('株価基準')
  },
)

it.each(['normal', 'unresolved', 'other-holding'])(
  'distinguishes a reported full sale from confirmed zero (%s)', mode => {
    const unresolved = mode !== 'normal'
    const data: DashboardView = {
      ...dashboard(), holding_quantity_unresolved: unresolved,
      valuation_error: unresolved ? '2331: 保有中の株数変更を照合できません。' : null,
      total_capital_yen: unresolved ? null : 10_309_500,
      holdings_market_value_yen: unresolved ? null : 0,
      realized_gross_pnl_yen: unresolved ? null : -93_000,
      available_cash_yen: 10_190_500, reserved_cash_yen: 119_000, reservations: [pendingBuy],
      holdings: mode === 'other-holding' ? [{
        ticker: '5678', company_name: null, sector: 'サービス業', quantity: 100,
        deployed_cost_yen: 99_000, market_price_yen: null, market_price_as_of: null,
        market_value_yen: null, unrealized_pnl_yen: null, unrealized_pnl_pct: null,
      }] : [],
    }
    hooks.values.set(0, data)
    hooks.values.set(1, { error: null, outcomes: [{
      outcome_id: 'retained-outcome', horizon: '1y', period_start_date: '2025-01-01',
      period_end_date: '2025-12-31', status: 'resolved', reason: null,
      portfolio_twr_pct: 10.2, benchmark_cumulative_return_pct: 8.2,
    }] })
    const html = render()
    expect(html).toContain('￥10,190,500')
    expect(html).toContain('￥119,000')
    expect(html).toContain('8929')
    expect(html).toContain('retained-outcome')
    if (unresolved) {
      expect(html).toContain('全売却後の保有株数を確認できません')
      expect(html).toContain('保有株数は未照合')
      expect(html).not.toContain('保有銘柄はありません')
      expect(html).not.toContain('銘柄を保有')
      expect(html).not.toContain('￥10,309,500')
      expect(html).not.toContain('-￥93,000')
      if (mode === 'other-holding') expect(html).toContain('5678')
    } else {
      expect(html).toContain('保有銘柄はありません')
      expect(html).toContain('0 銘柄を保有')
      expect(html).toContain('￥10,309,500')
      expect(html).toContain('-￥93,000')
      expect(html).not.toContain('保有株数は未照合')
    }
  },
)

it.each(['holding_quantity_unresolved', 'realized_gross_pnl_yen'] as const)(
  'rejects a dashboard missing the required %s without hiding outcomes', async field => {
    const payload: Partial<DashboardView> = dashboard()
    delete payload[field]
    const outcomes = { error: null, outcomes: [] }
    vi.mocked(fetchJson).mockResolvedValueOnce(payload).mockResolvedValueOnce(outcomes)
    render()
    hooks.effects[0]()
    await vi.waitFor(() => {
      expect(hooks.setters[3]).toHaveBeenCalledWith('資産状況の表示データが不正です。')
      expect(hooks.setters[1]).toHaveBeenCalledWith(outcomes)
    })
    expect(hooks.setters[0]).not.toHaveBeenCalled()
    hooks.values.set(3, hooks.setters[3].mock.calls[0][0])
    hooks.values.set(1, outcomes)
    hooks.setters = []
    hooks.effects = []
    const html = render()
    expect(html).toContain('資産・保有・未約定注文を読み込めません')
    expect(html).toContain('運用成績はまだありません')
    expect(html).not.toContain('保有銘柄はありません')
  },
)

it.each(['unresolved-reentry', 'quote-missing', 'normal'])(
  'labels a nonempty holding table and its download by quantity status (%s)', state => {
    const uncertain = state === 'unresolved-reentry'
    const hasQuote = state === 'normal'
    const data: DashboardView = {
      ...dashboard(), holding_quantity_unresolved: state === 'unresolved-reentry',
      valuation_error: state === 'unresolved-reentry' ? '2331: 保有中の株数変更を照合できません。' : null,
      holdings: [{
        ticker: '2331', company_name: '表示中の例', sector: 'サービス業', quantity: 125,
        deployed_cost_yen: 125_000, market_price_yen: hasQuote ? '1200' : null,
        market_price_as_of: hasQuote ? '2026-10-15' : null,
        market_value_yen: hasQuote ? 150_000 : null,
        unrealized_pnl_yen: hasQuote ? 25_000 : null, unrealized_pnl_pct: hasQuote ? 20 : null,
      }],
    }
    hooks.values.set(0, data)
    const html = render()
    expect(html).toContain('2331')
    expect(html).toMatch(/data-slot="table-cell"[^>]*>125<\/td>/)
    if (uncertain) {
      expect(html).toMatch(/role="heading"[^>]*>台帳上の保有記録<\/div>/)
      expect(html).toMatch(/data-slot="badge"[^>]*>表示 1 行<\/span>/)
      expect(html).toMatch(/data-slot="table-head"[^>]*>記録株数<\/th>/)
      expect(html).toContain('実際の保有銘柄・株数を確定した一覧ではありません。')
      expect(html).toContain('記録行の評価損益')
      expect(html).toContain('表示中の銘柄のみをTradingView用TXTに出力')
      expect(html).not.toMatch(/role="heading"[^>]*>保有銘柄<\/div>/)
      expect(html).not.toMatch(/data-slot="badge"[^>]*>1 銘柄<\/span>/)
      expect(html).not.toContain('評価損益 合計')
      expect(html).not.toContain('保有銘柄をTradingView用TXTに出力')
      expect(html).toContain('保有株数は未照合')
    } else {
      expect(html).toMatch(/role="heading"[^>]*>保有銘柄<\/div>/)
      expect(html).toMatch(/data-slot="badge"[^>]*>1 銘柄<\/span>/)
      expect(html).toMatch(/data-slot="table-head"[^>]*>数量<\/th>/)
      expect(html).toContain('評価損益 合計')
      expect(html).toContain('保有銘柄をTradingView用TXTに出力')
      expect(html).not.toContain('記録株数')
      if (state === 'quote-missing') expect(html).toContain('未評価: 2331')
    }
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
  expect(html).toContain('資産・保有・未約定注文の表示データ生成')
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
  if (outcomes.length) expect(html).toContain('<time dateTime="2025-01-01">2025-01-01</time> ～ <time dateTime="2025-12-31">2025-12-31</time>')
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


it('distinguishes asset display generation, last ledger record, and quote basis', () => {
  hooks.values.set(0, { ...dashboard(),
    generated_at: '2026-10-16T13:43:00Z',
    ledger_as_of: '2026-10-12T01:30:00Z',
    valuation_as_of: '2026-10-15',
  })
  const html = render()
  expect(html).toMatch(/資産・保有・未約定注文の表示データ生成 <time[^>]*dateTime="2026-10-16T13:43:00Z"[^>]*>2026-10-16 22:43 JST/)
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
    expect(html).not.toContain('保有銘柄はありません')
    expect(html).not.toContain('銘柄を保有')
  }
})

it.each(['request', 'saved-view'])('keeps the asset clock visible during an outcomes %s error', failure => {
  hooks.values.set(0, dashboard())
  if (failure === 'request') hooks.values.set(2, '保存済み運用成績を読み取れません。')
  else hooks.values.set(1, { error: '保存済み運用成績を読み取れません。', outcomes: [] })
  const html = render()
  expect(html).toContain('資産と損益')
  expect(html).toContain('資産・保有・未約定注文の表示データ生成')
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


it.each([
  { mode: 'retained-period', start: '2024-12-31', end: '2025-12-31' },
  { mode: 'current-period', start: '2025-10-15', end: '2026-10-15' },
])('shows the saved outcome period separately from the asset stamp ($mode)', ({ start, end }) => {
  hooks.values.set(0, { ...dashboard(), total_capital_yen: 12_345_678 })
  hooks.values.set(1, {
    error: null,
    outcomes: [{
      outcome_id: 'synthetic-outcome', horizon: '1y', period_start_date: start,
      period_end_date: end, status: 'resolved', reason: null,
      portfolio_twr_pct: 10.2, benchmark_cumulative_return_pct: 8.2,
    }],
  })
  const html = render()
  expect(html).toContain('￥12,345,678')
  expect(html).toMatch(/資産・保有・未約定注文の表示データ生成 <time[^>]*>2026-10-16 09:00 JST/)
  expect(html).toContain('保存した比較期間の成績です。資産・保有欄の生成時刻とは別です。')
  expect(html).toContain(`<time dateTime="${start}">${start}</time> ～ <time dateTime="${end}">${end}</time>`)
  expect(html).toContain('synthetic-outcome')
  expect(html).toContain('10.2%')
  expect(html).not.toContain('運用成績を読み込めません')
})

describe('wire-level outcome failures are local to the outcome view', () => {
  const validRow = {
    outcome_id: 'saved-fixture', horizon: '1y', period_start_date: '2025-01-01',
    period_end_date: '2025-12-31', status: 'resolved', reason: null,
    portfolio_twr_pct: 0, benchmark_cumulative_return_pct: null,
  }

  async function loadWireOutcome(wire: string): Promise<string> {
    // Keep the real JSON parser and its response→effect→render path. Only fetch is local.
    const actual = await vi.importActual<typeof import('../src/api/client')>('../src/api/client')
    vi.mocked(fetchJson).mockImplementation(actual.fetchJson)
    vi.stubGlobal('localStorage', { getItem: () => null })
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      if (path === '/api/dashboard') return Response.json({
        ...dashboard(), available_cash_yen: 123_456, reserved_cash_yen: 119_000,
        holdings: [{
          ticker: '2331', company_name: '合成保有', sector: 'サービス業', quantity: 100,
          deployed_cost_yen: 99_000, market_price_yen: null, market_price_as_of: null,
          market_value_yen: null, unrealized_pnl_yen: null, unrealized_pnl_pct: null,
        }],
        reservations: [pendingBuy],
      })
      if (path === '/api/outcomes') return new Response(wire, {
        status: 200, headers: { 'Content-Type': 'application/json' },
      })
      throw new Error('unexpected fixture request')
    }))
    render()
    hooks.effects[0]()
    await vi.waitFor(() => {
      expect(hooks.setters[0]).toHaveBeenCalledOnce()
      expect(hooks.setters[1].mock.calls.length + hooks.setters[2].mock.calls.length).toBe(1)
    })
    hooks.setters.forEach((setter, index) => {
      if (setter.mock.calls.length) hooks.values.set(index, setter.mock.calls[0][0])
    })
    hooks.setters = []
    hooks.effects = []
    return render()
  }

  it.each([
    ['null envelope', 'null'],
    ['the other view', '{"generated_at":"2026-11-22T18:00:00+09:00","holdings":[]}'],
    ['null row', '{"error":null,"outcomes":[null]}'],
    ['missing error in empty envelope', '{"outcomes":[]}'],
    ['missing error in nonempty envelope', JSON.stringify({ outcomes: [validRow] })],
    ['invalid return', JSON.stringify({ error: null, outcomes: [{ ...validRow, portfolio_twr_pct: '0' }] })],
    ['overflowing JSON number', JSON.stringify({ error: null, outcomes: [validRow] }).replace('"portfolio_twr_pct":0', '"portfolio_twr_pct":1e999')],
    ['invalid error shape', '{"error":{"invalid":true},"outcomes":[]}'],
  ])('preserves the other view after HTTP 200 with %s', async (_name, wire) => {
    const html = await loadWireOutcome(wire)
    expect(html).toContain('資産と損益')
    expect(html).toContain('￥123,456')
    expect(html).toContain('合成保有')
    expect(html).toContain('8929')
    expect(html).toContain('運用成績を読み込めません')
    expect(html).toContain('保存済み運用成績の表示データが不正です。')
    expect(html).not.toContain('運用成績はまだありません')
    expect(html).not.toContain('資産・保有・未約定注文を読み込めません')
  })

  it.each([
    { payload: { error: null, outcomes: [] }, text: '運用成績はまだありません' },
    { payload: { error: null, outcomes: [validRow] }, text: '0.0%' },
    { payload: { error: null, outcomes: [{ ...validRow, status: 'constructor' }] }, text: 'constructor' },
    { payload: { error: '保存済み運用成績を読み取れません。', outcomes: [] }, text: '保存済み運用成績を読み取れません。' },
  ])('keeps the current good envelope and a declared read error ($text)', async ({ payload, text }) => {
    const html = await loadWireOutcome(JSON.stringify(payload))
    expect(html).toContain(text)
    expect(html).toContain('￥123,456')
    expect(html).not.toContain('保存済み運用成績の表示データが不正です。')
  })
})
