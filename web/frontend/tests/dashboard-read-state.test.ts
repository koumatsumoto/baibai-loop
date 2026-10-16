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
    ledger_error: null, ledger_as_of: null, ledger_stale: false,
    valuation_as_of: null, valuation_stale: false,
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
  expect(html).toContain('前回実行からの変化')
})

it('keeps portfolio information visible when the independent outcomes request fails', () => {
  hooks.values.set(0, dashboard())
  hooks.values.set(2, 'outcomes unavailable')
  const html = render()
  expect(html).toContain('資産と損益')
  expect(html).toContain('保有銘柄はありません')
  expect(html).toContain('運用成績を読み込めません')
  expect(html).not.toContain('Dashboard read error')
})

it('routes an operations request failure to its own state, not the portfolio error', async () => {
  vi.mocked(fetchJson)
    .mockResolvedValueOnce(dashboard())
    .mockRejectedValueOnce(new Error('outcomes unavailable'))
    .mockReturnValueOnce(new Promise(() => {}))
  DashboardPage()
  hooks.effects[0]()
  await Promise.resolve()
  await Promise.resolve()
  expect(hooks.setters[2]).toHaveBeenCalledWith('outcomes unavailable')
  expect(hooks.setters[5]).not.toHaveBeenCalled()
  expect(hooks.setters[0]).toHaveBeenCalledWith(dashboard())
})
