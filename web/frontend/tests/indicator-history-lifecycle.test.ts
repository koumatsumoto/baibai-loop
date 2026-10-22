import { createElement, isValidElement, type ReactElement, type ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError, fetchJson } from '../src/api/client'
import { MarketContextPage } from '../src/pages/MarketContextPage'

const hooks = vi.hoisted(() => ({
  effects: [] as { setup: () => (() => void) | undefined; dependencies: unknown[] }[],
  setters: [] as ReturnType<typeof vi.fn>[],
  values: new Map<number, unknown>(),
}))
vi.mock('react', async (original) => ({
  ...await original<typeof import('react')>(),
  useEffect: (setup: () => (() => void) | undefined, dependencies: unknown[]) => {
    hooks.effects.push({ setup, dependencies })
  },
  useState: (initial: unknown) => {
    const index = hooks.setters.length
    const setter = vi.fn()
    hooks.setters.push(setter)
    return [hooks.values.has(index) ? hooks.values.get(index) : initial, setter]
  },
}))
vi.mock('../src/api/client', async (original) => ({
  ...await original<typeof import('../src/api/client')>(), fetchJson: vi.fn(),
}))
vi.mock('../src/components/LoadingIndicator', () => ({ LoadingPage: () => null, LoadingIndicator: () => null }))

function deferred() {
  let resolve!: (value: unknown) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail })
  return { promise, resolve, reject }
}
async function settle() {
  await Promise.resolve()
  await Promise.resolve()
  await Promise.resolve()
}
function start(id: string | null) {
  hooks.setters = []
  hooks.values.set(6, id)
  MarketContextPage()
  const effect = hooks.effects.at(-1)!
  expect(effect.dependencies).toEqual([id])
  const cleanup = effect.setup()
  const setters = hooks.setters.slice(7)
  expect(setters.map(s => s.mock.calls)).toEqual([[[null]], [[id !== null]], [[false]]])
  return { cleanup, setters }
}
afterEach(() => {
  vi.resetAllMocks()
  hooks.effects = []
  hooks.setters = []
  hooks.values.clear()
})

describe('Market Context series history lifecycle', () => {
  it.each(['success', '404', 'error', 'abort'])('keeps B after old A %s, including close/open', async outcome => {
    const a = deferred(), b = deferred()
    vi.mocked(fetchJson).mockReturnValueOnce(a.promise).mockReturnValueOnce(b.promise)
    const first = start('us.cpi_yoy')
    const signal = vi.mocked(fetchJson).mock.calls[0][1]!.signal!
    first.cleanup!()
    expect(signal.aborted).toBe(true)
    start(null)
    const second = start('jp.cpi_yoy')
    b.resolve({ series_id: 'jp.cpi_yoy' })
    await settle()
    expect(second.setters[0]).toHaveBeenLastCalledWith({ series_id: 'jp.cpi_yoy' })
    expect(second.setters[1]).toHaveBeenLastCalledWith(false)
    if (outcome === 'success') a.resolve({ series_id: 'us.cpi_yoy' })
    else a.reject(outcome === '404' ? new ApiError(404, 'Not Found') : outcome === 'abort' ? new DOMException('Cancelled', 'AbortError') : new Error('old error'))
    await settle()
    expect(first.setters.map(s => s.mock.calls)).toEqual([[[null]], [[true]], [[false]]])
    expect(second.setters[0]).toHaveBeenCalledTimes(2)
    expect(second.setters[2]).toHaveBeenCalledTimes(1)
    second.cleanup!()
  })
  it('shows current failure and clears it for the next series', async () => {
    vi.mocked(fetchJson).mockRejectedValueOnce(new ApiError(404, 'Not Found'))
    const current = start('us.cpi_yoy')
    await settle()
    expect(current.setters[2]).toHaveBeenLastCalledWith(true)
    expect(current.setters[1]).toHaveBeenLastCalledWith(false)
    current.cleanup!()
    vi.mocked(fetchJson).mockReturnValueOnce(new Promise(() => {}))
    start('jp.cpi_yoy').cleanup!()
  })
  it('does not display AbortError as failure', async () => {
    vi.mocked(fetchJson).mockRejectedValueOnce(new DOMException('Cancelled', 'AbortError'))
    const current = start('us.cpi_yoy')
    await settle()
    expect(current.setters[2].mock.calls).toEqual([[false]])
    current.cleanup!()
  })
})

function elements(node: ReactNode): ReactElement<Record<string, unknown>>[] {
  if (Array.isArray(node)) return node.flatMap(elements)
  if (!isValidElement<Record<string, unknown>>(node)) return []
  return [node, ...elements(node.props.children as ReactNode)]
}
function text(node: ReactNode): string {
  if (Array.isArray(node)) return node.map(text).join('')
  if (isValidElement<{ children?: ReactNode }>(node)) return text(node.props.children)
  return typeof node === 'string' || typeof node === 'number' ? String(node) : ''
}
it.each([true, false])('does not render a fallback chart or selected-period caption without history (error=%s)', failure => {
  hooks.values.set(0, { groups: [], reports: [] })
  const tree = MarketContextPage()
  const dialog = elements(tree).find(e => typeof e.type === 'function' && e.type.name === 'IndicatorDialog')!
  expect(dialog).toBeDefined()
  const render = dialog.type as (props: Record<string, unknown>) => ReactNode
  const detail = render({ ...dialog.props, row: {
    series: { name: 'CPI', series_id: 'us.cpi_yoy', unit: '%', tradingview_symbol: null, values: [1, 2] },
    reading: null, failedFetch: null,
  }, history: null, historyLoading: false, historyError: failure, period: '10y', granularity: 'daily' })
  expect(elements(detail).some(e => typeof e.type === 'function' && e.type.name === 'FullChart')).toBe(false)
  expect(text(detail)).not.toContain('チャートは')
  expect(text(detail).includes('系列履歴を取得できませんでした')).toBe(failure)
})

it.each([45, 46])('keeps the v5 date, age, stale badge and detail link at %s days', age => {
  const contextId = 'macro-context-2026-07-17-small'
  hooks.values.set(0, {
    groups: [], reports: [], reading: null,
    latest_context: {
      context_id: contextId, as_of: '2026-07-17', age_days: age, stale: age > 45,
      published_at: '2026-07-19T12:00:00+09:00', summary: '保存済みの市場環境判断', warnings: [],
      assessment: {
        known_at: '2026-07-19T11:00:00+09:00', changes: [], counter_evidence: [],
        uncertainties: [], implications: [], inputs: [],
      },
    },
  })
  const latest = elements(MarketContextPage()).find(e => typeof e.type === 'function' && e.type.name === 'LatestContext')!
  expect(latest).toBeDefined()
  const html = renderToStaticMarkup(createElement(MemoryRouter, {}, latest))
  expect(html).toContain('2026-07-17')
  expect(html).toContain(`${age} 日前`)
  expect(html).toContain(`href="/market-context/assessments/${contextId}"`)
  expect(html).toContain('保存済みの市場環境判断')
  expect(html.includes('>stale<')).toBe(age > 45)
})


it('does not render the previous series history during a new selection', () => {
  hooks.values.set(0, { groups: [], reports: [], reading: null })
  const dialog = elements(MarketContextPage()).find(e => typeof e.type === 'function' && e.type.name === 'IndicatorDialog')!
  const render = dialog.type as (props: Record<string, unknown>) => ReactNode
  const detail = render({ ...dialog.props, row: {
    series: { name: 'Japan CPI', series_id: 'jp.cpi_yoy', unit: '%', tradingview_symbol: null },
    reading: null, failedFetch: null,
  }, history: {
    name: 'US CPI', series_id: 'us.cpi_yoy', unit: '%', tradingview_symbol: null,
    points: [{ observed_at: '2026-10-01', value: 2 }],
  }, historyLoading: false, historyError: false, period: 'max', granularity: 'monthly' })
  expect(text(detail)).toContain('Japan CPI')
  expect(text(detail)).not.toContain('US CPI')
  expect(elements(detail).some(e => typeof e.type === 'function' && e.type.name === 'FullChart')).toBe(false)
})

it('opens the dialog with materialized reading JSON from before observation notes were added', () => {
  hooks.values.set(0, { groups: [], reports: [], reading: null })
  const dialog = elements(MarketContextPage()).find(e => typeof e.type === 'function' && e.type.name === 'IndicatorDialog')!
  const render = dialog.type as (props: Record<string, unknown>) => ReactNode
  const reading = JSON.parse(`{
    "series_id": "us.cpi_yoy", "name": "CPI", "category": "inflation",
    "geography": "us", "frequency": "monthly", "unit": "percent",
    "latest_value": 2.4, "observed_at": "2026-10-01", "staleness_days": 21,
    "stale": false, "staleness_warn_days": 45,
    "next_print_estimate": null, "print_due_in_days": null,
    "window_years": 10, "window_observations": 120, "expected_observations": 120,
    "insufficient_history": false, "statistic": "level", "statistic_unit": "percent",
    "statistic_value": 2.4, "percentile": 0.5, "z_score": 0,
    "short_trend": null, "long_trend": null, "flags": [], "read_semantics": null
  }`)
  const detail = render({ ...dialog.props, row: {
    series: { name: 'CPI', series_id: 'us.cpi_yoy', unit: 'percent', tradingview_symbol: null },
    reading, failedFetch: null, statuses: [],
  }, history: null, historyLoading: false, historyError: false, period: 'max', granularity: 'monthly' })
  expect(text(detail)).toContain('CPI')
  expect(text(detail)).toContain('2026-10-01')
  expect(text(detail)).not.toContain('選択保存原本の受領時刻')
})
