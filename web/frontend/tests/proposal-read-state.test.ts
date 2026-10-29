import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, expect, it, vi } from 'vitest'

import { fetchJson } from '../src/api/client'
import type { LatestProposalView } from '../src/api/types'
import { ProposalPage } from '../src/pages/ProposalPage'
import { proposalViewFixture } from './fixtures/proposals'

const hooks = vi.hoisted(() => ({
  values: new Map<number, unknown>(),
  setters: [] as ReturnType<typeof vi.fn>[],
  effects: [] as (() => () => void)[],
}))
vi.mock('react', async original => ({
  ...await original<typeof import('react')>(),
  useState: (initial: unknown) => {
    const index = hooks.setters.length
    const setter = vi.fn()
    hooks.setters.push(setter)
    return [hooks.values.has(index) ? hooks.values.get(index) : initial, setter]
  },
  useEffect: (setup: () => () => void) => { hooks.effects.push(setup) },
}))
vi.mock('../src/api/client', () => ({ fetchJson: vi.fn() }))

function resetHooks() {
  hooks.values.clear()
  hooks.setters = []
  hooks.effects = []
}

afterEach(() => {
  vi.resetAllMocks()
  resetHooks()
})

it('loads only the saved report through the normal API client', async () => {
  const view = proposalViewFixture()
  vi.mocked(fetchJson).mockResolvedValue(view)
  expect(renderToStaticMarkup(ProposalPage())).toContain('売買提案を読み込んでいます')
  const cleanup = hooks.effects[0]()
  expect(fetchJson).toHaveBeenCalledExactlyOnceWith('/api/proposal/latest', { signal: expect.any(AbortSignal) })
  await Promise.resolve()
  expect(hooks.setters[0]).toHaveBeenCalledExactlyOnceWith(view)
  expect(hooks.setters[1]).not.toHaveBeenCalled()
  cleanup()
})

it('keeps a transport failure distinct from a valid empty report', async () => {
  vi.mocked(fetchJson).mockRejectedValue(new Error('transport failed'))
  ProposalPage()
  hooks.effects[0]()
  await Promise.resolve()
  await Promise.resolve()
  expect(hooks.setters[1]).toHaveBeenCalledWith('transport failed')
  resetHooks()
  hooks.values.set(1, 'transport failed')
  const html = renderToStaticMarkup(ProposalPage())
  expect(html).toContain('売買提案の通信エラー')
  expect(html).toContain('transport failed')
  expect(html).not.toContain('保存済み提案はありません')
})

it.each(['success', 'failure'] as const)('ignores an old %s after leaving and re-entering the page', async result => {
  let resolveOld!: (view: LatestProposalView) => void
  let rejectOld!: (error: Error) => void
  vi.mocked(fetchJson).mockReturnValueOnce(new Promise((resolve, reject) => {
    resolveOld = resolve
    rejectOld = reject
  }))
  ProposalPage()
  const oldSetters = [...hooks.setters]
  const cleanup = hooks.effects[0]()
  const oldSignal = vi.mocked(fetchJson).mock.calls[0][1]?.signal
  cleanup()
  expect(oldSignal?.aborted).toBe(true)

  resetHooks()
  const newView: LatestProposalView = { ...proposalViewFixture(), status: 'empty', proposal: null }
  vi.mocked(fetchJson).mockResolvedValueOnce(newView)
  ProposalPage()
  hooks.effects[0]()
  await Promise.resolve()
  if (result === 'success') resolveOld(proposalViewFixture())
  else rejectOld(new Error('obsolete request'))
  await Promise.resolve()
  await Promise.resolve()
  expect(oldSetters[0]).not.toHaveBeenCalled()
  expect(oldSetters[1]).not.toHaveBeenCalled()
  expect(hooks.setters[0]).toHaveBeenCalledExactlyOnceWith(newView)
  expect(hooks.setters[1]).not.toHaveBeenCalled()
  expect(fetchJson).toHaveBeenCalledTimes(2)
})
