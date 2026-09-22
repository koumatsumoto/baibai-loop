import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError, fetchJson } from '../src/api/client'
import { CapitalAllocationAssessmentPage } from '../src/pages/CapitalAllocationAssessmentPage'
import { MacroReportPage } from '../src/pages/MacroReportPage'
import { SecurityDetailPage } from '../src/pages/SecurityDetailPage'

const hooks = vi.hoisted(() => ({
  effects: [] as { setup: () => () => void; dependencies: unknown[] }[],
  setters: [] as ReturnType<typeof vi.fn>[],
  params: {} as Record<string, string>,
}))

// Run the real page effects with controlled requests, without adding a DOM renderer.
vi.mock('react', async (importOriginal) => ({
  ...await importOriginal<typeof import('react')>(),
  useEffect: (setup: () => () => void, dependencies: unknown[]) => {
    hooks.effects.push({ setup, dependencies })
  },
  useState: (initial: unknown) => {
    const setter = vi.fn()
    hooks.setters.push(setter)
    return [initial, setter]
  },
}))
vi.mock('react-router', async (importOriginal) => ({
  ...await importOriginal<typeof import('react-router')>(),
  useParams: () => hooks.params,
}))
vi.mock('../src/api/client', async (importOriginal) => ({
  ...await importOriginal<typeof import('../src/api/client')>(),
  fetchJson: vi.fn(),
}))
vi.mock('../src/components/LoadingIndicator', () => ({ LoadingPage: () => null }))

function deferred() {
  let resolve!: (value: unknown) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail })
  return { promise, resolve, reject }
}

async function settle() {
  await Promise.resolve()
  await Promise.resolve()
}

afterEach(() => {
  vi.resetAllMocks()
  hooks.effects = []
  hooks.setters = []
})

const pages = [
  { name: 'security', render: SecurityDetailPage, param: 'ticker', path: '/api/securities/', states: [null, false, null] },
  { name: 'macro', render: MacroReportPage, param: 'contextId', path: '/api/macro/context/', states: [null, null] },
  { name: 'CAA', render: CapitalAllocationAssessmentPage, param: 'capitalAllocationAssessmentId', path: '/api/capital-allocation-assessments/', states: [null, null] },
]

for (const page of pages) {
  describe(`${page.name} request lifecycle`, () => {
    function start(id: string) {
      hooks.params = { [page.param]: id }
      hooks.setters = []
      page.render()
      const effect = hooks.effects.at(-1)!
      expect(effect.dependencies).toEqual([id])
      const cleanup = effect.setup()
      const setters = hooks.setters
      expect(setters.map((setter) => setter.mock.calls)).toEqual(page.states.map((state) => [[state]]))
      const [url, init] = vi.mocked(fetchJson).mock.calls.at(-1)!
      expect(url).toBe(`${page.path}${encodeURIComponent(id)}`)
      expect(init?.signal).toBeInstanceOf(AbortSignal)
      expect(init?.signal?.aborted).toBe(false)
      return { cleanup, setters, signal: init!.signal! }
    }

    it.each(['success', '404', 'error', 'abort'] as const)('ignores old %s after B succeeds', async (outcome) => {
      const a = deferred()
      const b = deferred()
      vi.mocked(fetchJson).mockReturnValueOnce(a.promise).mockReturnValueOnce(b.promise)
      const first = start('A')
      first.cleanup()
      expect(first.signal.aborted).toBe(true)
      const second = start('B /')
      expect(second.signal).not.toBe(first.signal)
      b.resolve({ id: 'B' })
      await settle()
      expect(second.setters[0]).toHaveBeenLastCalledWith({ id: 'B' })
      // Ignore abort in the mock to exercise callbacks for an already-completed response.
      if (outcome === 'success') a.resolve({ id: 'A' })
      else a.reject(outcome === '404' ? new ApiError(404, 'Not Found') : outcome === 'abort' ? new DOMException('Cancelled', 'AbortError') : new Error('old error'))
      await settle()
      expect(first.setters.map((setter) => setter.mock.calls)).toEqual(page.states.map((state) => [[state]]))
      expect(second.setters.map((setter) => setter.mock.calls)).toEqual(page.states.map((state, index) => index === 0 ? [[state], [{ id: 'B' }]] : [[state]]))
      second.cleanup()
    })

    it.each([404, 500])('preserves current HTTP %s handling and resets on the next ID', async (status) => {
      const request = deferred()
      vi.mocked(fetchJson).mockReturnValueOnce(request.promise)
      const current = start('A')
      const reason = new ApiError(status, 'Failed')
      request.reject(reason)
      await settle()
      const errorIndex = page.states.length - 1
      if (page.name === 'security' && status === 404) {
        expect(current.setters[1]).toHaveBeenLastCalledWith(true)
        expect(current.setters[errorIndex]).toHaveBeenCalledTimes(1)
      } else {
        expect(current.setters[errorIndex]).toHaveBeenLastCalledWith(reason.message)
      }
      current.cleanup()
      vi.mocked(fetchJson).mockReturnValueOnce(new Promise(() => {}))
      start('B').cleanup()
    })

    it('ignores AbortError without displaying an error', async () => {
      vi.mocked(fetchJson).mockRejectedValueOnce(new DOMException('Cancelled', 'AbortError'))
      const current = start('A')
      await settle()
      expect(current.setters.map((setter) => setter.mock.calls)).toEqual(page.states.map((state) => [[state]]))
      current.cleanup()
    })
  })
}
