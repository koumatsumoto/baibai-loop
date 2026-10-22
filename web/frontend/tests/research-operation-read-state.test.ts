import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'

import { fetchJson } from '../src/api/client'
import { fetchProposalHistory } from '../src/api/proposal-history'
import type { InvestmentProposalsView, OperationsView } from '../src/api/types'
import { ResearchPage } from '../src/pages/ResearchPage'

const hooks = vi.hoisted(() => ({
  cursor: 0,
  values: new Map<number, unknown>(),
  effects: [] as (() => (() => void) | undefined)[],
}))
vi.mock('react', async original => ({
  ...await original<typeof import('react')>(),
  useState: (initial: unknown) => {
    const index = hooks.cursor++
    return [hooks.values.has(index) ? hooks.values.get(index) : initial, (value: unknown) => hooks.values.set(index, value)]
  },
  useEffect: (setup: () => (() => void) | undefined) => { hooks.effects.push(setup) },
}))
vi.mock('../src/api/client', () => ({ fetchJson: vi.fn() }))
vi.mock('../src/api/proposal-history', () => ({ fetchProposalHistory: vi.fn() }))
vi.mock('../src/components/LoadingIndicator', () => ({ LoadingPage: () => null }))

function render() {
  hooks.cursor = 0
  return renderToStaticMarkup(createElement(MemoryRouter, null, ResearchPage()))
}
afterEach(() => {
  vi.resetAllMocks()
  hooks.values.clear()
  hooks.effects = []
})

it('全対象unfinishedで終了した調査と保存defer提案を同じResearch入口で区別する', () => {
  const proposalId = 'investment-proposal-synthetic-defer'
  const operationId = 'research-op-00000000000040008000000000000001'
  const proposals: InvestmentProposalsView = { proposals: [{
    proposal_id: proposalId, as_of: '2026-10-14', published_at: '2026-10-14T19:00:00+09:00',
    request_scope: 'discover_new', headline: '資料不足のため再開条件を残す',
    item_count: 1, tickers: ['1234'], actions: ['defer'], research_operation_id: operationId,
  }], next_cursor: null }
  const operations: OperationsView = { outcomes: [], operations: [{
    operation_id: operationId, session_kind: 'capital-allocation', status: 'completed',
    as_of: '2026-10-14', ticker: null, started_at: '2026-10-14T18:00:00+09:00', completed_at: '2026-10-14T19:00:00+09:00',
    payload: { checkpoint: 'synthetic result', result: '資料不足のため調査終了', next: '開示後に新しい明示依頼で再確認' },
    research: {
      request: 'synthetic explicit request', request_scope: 'discover_new', selection_summary: '合成選定理由',
      max_deep_research_targets: 3, proposal_id: proposalId, proposal_draft_sha256: 'a'.repeat(64),
      source_run_revision_id: null, source_analysis_as_of: null,
      targets: [{ ticker: '1234', purpose: 'new', outcome_status: 'unfinished', assessment_ref: null,
        unfinished_reason: '重要な一次資料の確認待ち', references: ['synthetic-source'],
        selection_rationale: null, research_question: null, key_risk: null }],
    },
  }] }
  hooks.values.set(0, proposals)
  hooks.values.set(2, operations)
  const html = render()
  for (const text of ['判断保留', '>調査終了</p>', '企業評価・Review完了 0件 · 調査未完了 1件', '保存提案あり', `/stocks/investment-proposals/${proposalId}`, '重要な一次資料の確認待ち']) expect(html).toContain(text)
  expect(html).not.toContain('提案工程完了')
  expect(html).not.toContain('人間確認済み')
})

it.each(['empty', 'failed', 'pending'])('Researchの進行記録は取得済み0件と通信失敗・未取得を区別する: %s', async outcome => {
  vi.mocked(fetchProposalHistory).mockResolvedValue({ proposals: [], next_cursor: null })
  vi.mocked(fetchJson).mockResolvedValueOnce({ holdings: [] })
  if (outcome === 'empty') vi.mocked(fetchJson).mockResolvedValueOnce({ operations: [], outcomes: [] })
  else if (outcome === 'failed') vi.mocked(fetchJson).mockRejectedValueOnce(new Error('synthetic failure'))
  else vi.mocked(fetchJson).mockReturnValueOnce(new Promise(() => {}))
  render()
  const cleanup = hooks.effects[0]()
  await Promise.resolve()
  await Promise.resolve()
  await Promise.resolve()
  const html = render()
  expect(vi.mocked(fetchJson).mock.calls.map(([path]) => path)).toEqual(['/api/dashboard', '/api/operations'])
  if (outcome === 'empty') {
    expect(html).toContain('調査・運用の進行記録はまだありません')
    expect(html).not.toContain('調査進捗を読み込めませんでした')
    expect(html).not.toContain('調査進捗を読み込んでいます')
  } else {
    expect(html).not.toContain('調査・運用の進行記録はまだありません')
    if (outcome === 'failed') {
      expect(html).toContain('調査進捗を読み込めませんでした。記録がないことを意味しません。')
      expect(html).toContain('role="alert"')
    } else expect(html).toContain('調査進捗を読み込んでいます')
  }
  cleanup?.()
})
