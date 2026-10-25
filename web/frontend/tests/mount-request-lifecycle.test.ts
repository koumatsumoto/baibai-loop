import { afterEach, describe, expect, it, vi } from 'vitest'

import { fetchJson } from '../src/api/client'
import { AppShell } from '../src/components/AppShell'
import { CandidateExplorationPage } from '../src/pages/CandidateExplorationPage'
import { DashboardPage } from '../src/pages/DashboardPage'
import { InvestmentJudgmentPage } from '../src/pages/InvestmentJudgmentPage'
import { TasksPage } from '../src/pages/TasksPage'

const hooks = vi.hoisted(() => ({
  effects: [] as (() => (() => void) | undefined)[],
  setters: [] as ReturnType<typeof vi.fn>[],
}))
vi.mock('react', async original => ({
  ...await original<typeof import('react')>(),
  useEffect: (setup: () => (() => void) | undefined) => { hooks.effects.push(setup) },
  useState: (initial: unknown) => {
    const setter = vi.fn()
    hooks.setters.push(setter)
    return [initial, setter]
  },
}))
vi.mock('react-router', async original => ({
  ...await original<typeof import('react-router')>(),
  useLocation: () => ({ pathname: '/' }),
}))
vi.mock('../src/api/client', () => ({ fetchJson: vi.fn() }))
vi.mock('../src/api/proposal-history', () => ({
  fetchProposalHistory: vi.fn(() => new Promise(() => {})),
}))
vi.mock('../src/components/LoadingIndicator', () => ({
  LoadingPage: () => null, LoadingIndicator: () => null,
}))

function deferred() {
  let resolve!: (value: unknown) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail })
  return { promise, resolve, reject }
}
function payload(marker: string) {
  return { marker, holdings: [{ ticker: '1234', company_name: marker }] }
}
async function settle() {
  await Promise.resolve()
  await Promise.resolve()
  await Promise.resolve()
}
afterEach(() => {
  vi.clearAllMocks()
  hooks.effects = []
  hooks.setters = []
})

const pages: {
  name: string
  render: () => unknown
  requests: { path: string; setterIndex: number; expectedValue?: (marker: string) => unknown }[]
}[] = [
  { name: 'Dashboard', render: DashboardPage, requests: [
    { path: '/api/dashboard', setterIndex: 0 },
    { path: '/api/operations', setterIndex: 1 },
    { path: '/api/daily-delta', setterIndex: 3 },
  ] },
  { name: 'Tasks', render: TasksPage, requests: [
    { path: '/api/tasks', setterIndex: 0 },
  ] },
  { name: '候補探索', render: CandidateExplorationPage, requests: [
    { path: '/api/screening/latest', setterIndex: 0 },
  ] },
  { name: 'Research補助情報', render: InvestmentJudgmentPage, requests: [
    { path: '/api/dashboard', setterIndex: 1, expectedValue: marker => new Map([['1234', marker]]) },
    { path: '/api/operations', setterIndex: 2 },
  ] },
  { name: 'AppShell', render: AppShell, requests: [
    { path: '/api/meta', setterIndex: 0 },
  ] },
]

for (const page of pages) {
  describe(`${page.name}のmount request`, () => {
    it.each(['success', 'error', 'abort'])('StrictModeの再setup後に旧%sがstateを変更しない', async outcome => {
      const paths = page.requests.map(request => request.path)
      const previous = paths.map(() => deferred())
      const current = paths.map(() => deferred())
      for (const request of [...previous, ...current]) vi.mocked(fetchJson).mockReturnValueOnce(request.promise)
      page.render()
      const setup = hooks.effects[0]
      // StrictMode preserves state while it runs setup -> cleanup -> setup.
      const cleanup = setup()
      cleanup?.()
      const currentCleanup = setup()
      const currentValues = paths.map(path => payload(`current:${path}`))
      current.forEach((request, index) => request.resolve(currentValues[index]))
      await settle()
      const expectedCalls = hooks.setters.map(() => [] as unknown[][])
      page.requests.forEach((request, index) => {
        expectedCalls[request.setterIndex] = [[request.expectedValue?.(`current:${request.path}`) ?? currentValues[index]]]
      })
      expect(hooks.setters.map(setter => setter.mock.calls)).toEqual(expectedCalls)
      const completed = hooks.setters.map(setter => [...setter.mock.calls])
      // Deliberately let mocks settle after abort to check callbacks too.
      for (const request of previous) {
        if (outcome === 'success') request.resolve(payload('previous'))
        else request.reject(outcome === 'abort' ? new DOMException('Cancelled', 'AbortError') : new Error('old failure'))
      }
      await settle()
      expect(hooks.setters.map(setter => setter.mock.calls)).toEqual(completed)
      expect(vi.mocked(fetchJson).mock.calls.map(([path]) => path)).toEqual([...paths, ...paths])
      const signals = vi.mocked(fetchJson).mock.calls.map(([, init]) => init?.signal)
      expect(signals.slice(0, paths.length).every(signal => signal?.aborted === true)).toBe(true)
      expect(signals.slice(paths.length).every(signal => signal?.aborted === false)).toBe(true)
      currentCleanup?.()
    })
  })
}
