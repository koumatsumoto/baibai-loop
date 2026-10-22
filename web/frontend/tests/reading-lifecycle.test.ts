import { isValidElement, type ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchJson } from '../src/api/client'
import { useProposalReadingAnchor } from '../src/components/ReadingNavigation'
import { CurrentProposalPreviewPanel } from '../src/pages/InvestmentProposalPage'

const hooks = vi.hoisted(() => ({
  effects: [] as (() => void | (() => void))[],
  setters: [] as ReturnType<typeof vi.fn>[],
  hash: '#proposal-item-1234',
}))

vi.mock('react', async (importOriginal) => ({
  ...await importOriginal<typeof import('react')>(),
  useEffect: (setup: () => void | (() => void)) => { hooks.effects.push(setup) },
  useState: (initial: unknown) => {
    const setter = vi.fn()
    hooks.setters.push(setter)
    return [initial, setter]
  },
  useRef: (initial: unknown) => ({ current: initial }),
}))
vi.mock('react-router', async (importOriginal) => ({
  ...await importOriginal<typeof import('react-router')>(),
  useLocation: () => ({ hash: hooks.hash }),
}))
vi.mock('../src/api/client', () => ({ fetchJson: vi.fn() }))

function deferred() {
  let resolve!: (value: unknown) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail })
  return { promise, resolve, reject }
}

async function settle() {
  for (let turn = 0; turn < 5; turn++) await Promise.resolve()
}

function refreshAction(node: ReactNode): (() => void) | undefined {
  if (Array.isArray(node)) {
    for (const child of node) {
      const action = refreshAction(child)
      if (action) return action
    }
  } else if (isValidElement<{ children?: ReactNode; onClick?: () => void }>(node)) {
    if (node.props.onClick) return node.props.onClick
    return refreshAction(node.props.children)
  }
  return undefined
}

afterEach(() => {
  vi.resetAllMocks()
  vi.unstubAllGlobals()
  hooks.effects = []
  hooks.setters = []
  hooks.hash = '#proposal-item-1234'
})

describe('公開済み再試算の読込', () => {
  it('素早い二重操作を一つのGETへまとめ、完了後には再試行できる', async () => {
    const request = deferred()
    vi.mocked(fetchJson).mockReturnValue(request.promise)
    const refresh = refreshAction(CurrentProposalPreviewPanel({ proposalId: 'original' }))!
    hooks.effects[0]()
    refresh()
    refresh()
    expect(fetchJson).toHaveBeenCalledTimes(1)
    expect(vi.mocked(fetchJson).mock.calls[0][0]).toBe('/api/investment-proposals/original/current-preview')
    request.resolve({ saved_proposal_id: 'original' })
    await settle()
    expect(hooks.setters[0]).toHaveBeenLastCalledWith({ saved_proposal_id: 'original' })
    expect(hooks.setters[2]).toHaveBeenLastCalledWith(false)
    refresh()
    expect(fetchJson).toHaveBeenCalledTimes(2)
  })

  it.each(['success', 'error'] as const)('移動で中断した古い%sを表示へ戻さない', async outcome => {
    const request = deferred()
    vi.mocked(fetchJson).mockReturnValue(request.promise)
    const refresh = refreshAction(CurrentProposalPreviewPanel({ proposalId: 'original' }))!
    const cleanup = hooks.effects[0]() as () => void
    refresh()
    const signal = vi.mocked(fetchJson).mock.calls[0][1]!.signal!
    cleanup()
    expect(signal.aborted).toBe(true)
    if (outcome === 'success') request.resolve({ saved_proposal_id: 'old' })
    else request.reject(new Error('old request failed'))
    await settle()
    expect(hooks.setters[0]).not.toHaveBeenCalled()
    expect(hooks.setters[1].mock.calls).toEqual([[null]])
    expect(hooks.setters[2].mock.calls).toEqual([[true]])
  })

  it('現在の取得失敗を表示し、次の読込を妨げない', async () => {
    vi.mocked(fetchJson).mockRejectedValue(new Error('preview unavailable'))
    const refresh = refreshAction(CurrentProposalPreviewPanel({ proposalId: 'original' }))!
    refresh()
    await settle()
    expect(hooks.setters[0]).not.toHaveBeenCalled()
    expect(hooks.setters[1]).toHaveBeenLastCalledWith('preview unavailable')
    refresh()
    expect(fetchJson).toHaveBeenCalledTimes(2)
    await settle()
  })
})

describe('非同期に読み込んだ提案への復帰', () => {
  it('本文が揃ってから対象位置とfocusを戻す', () => {
    const target = { scrollIntoView: vi.fn(), focus: vi.fn() }
    const getElementById = vi.fn().mockReturnValue(target)
    vi.stubGlobal('document', { getElementById })
    useProposalReadingAnchor(false)
    hooks.effects.at(-1)!()
    expect(getElementById).not.toHaveBeenCalled()
    useProposalReadingAnchor(true)
    hooks.effects.at(-1)!()
    expect(getElementById).toHaveBeenCalledWith('proposal-item-1234')
    expect(target.scrollIntoView).toHaveBeenCalledWith({ block: 'start' })
    expect(target.focus).toHaveBeenCalledWith({ preventScroll: true })
  })

  it('直リンクや対象がない場合は別の要素へ移動しない', () => {
    const getElementById = vi.fn().mockReturnValue(null)
    vi.stubGlobal('document', { getElementById })
    hooks.hash = ''
    useProposalReadingAnchor(true)
    hooks.effects.at(-1)!()
    expect(getElementById).not.toHaveBeenCalled()
    hooks.hash = '#proposal-item-missing'
    useProposalReadingAnchor(true)
    hooks.effects.at(-1)!()
    expect(getElementById).toHaveBeenCalledWith('proposal-item-missing')
  })
})
