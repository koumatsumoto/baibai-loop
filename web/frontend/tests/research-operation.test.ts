import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { OperationSessionView } from '../src/api/types'
import { ResearchOperationCard } from '../src/pages/dashboard/ResearchOperationCard'

function session(targets: object[], outcomes: object[]): OperationSessionView {
  return {
    operation_id: 'op-20260907-capital-allocation-1', session_kind: 'capital-allocation', status: 'completed',
    as_of: '2026-09-07', ticker: null, started_at: '2026-09-07T18:00:00+09:00', completed_at: '2026-09-07T18:00:00+09:00',
    payload: { checkpoint: 'synthetic proposal complete', artifacts: [{ kind: 'research_selection', request: 'synthetic explicit request', targets }, { kind: 'research_progress', outcomes }], result: 'synthetic completion', human_confirmation: null },
  }
}
function render(operation: OperationSessionView) { return renderToStaticMarkup(createElement(ResearchOperationCard, { operations: [operation] })) }

describe('有限調査と提案の表示', () => {
  it('部分完了と保有提案を表示し、人間確認済み公開と分ける', () => {
    const html = render(session([{ ticker: '1234', purpose: 'new' }, { ticker: '5678', purpose: 'new' }, { ticker: '2331', purpose: 'holding' }], [{ ticker: '1234', status: 'reviewed' }, { ticker: '5678', status: 'unfinished', reason: '一次資料の確認未完了' }, { ticker: '2331', status: 'proposal', draft: { action: 'hold' }, checked_at: '2026-09-07T18:00:00+09:00' }]))
    expect(html).toContain('提案工程完了')
    expect(html).toContain('固定対象 3件')
    expect(html).toContain('調査未完了: 一次資料の確認未完了')
    expect(html).toContain('保有提案 hold（人間確認済み公開とは別）')
    expect(html).toContain('検証時点 2026-09-07T18:00:00+09:00')
  })
  it('0件と全未完了を提案なしとして表示する', () => {
    expect(render(session([], []))).toContain('提案なしで終了')
    expect(render(session([{ ticker: '1234', purpose: 'new' }], [{ ticker: '1234', status: 'unfinished', reason: '調査打切り' }]))).toContain('提案なしで終了')
  })
  it('activeを完了として表示しない', () => {
    const operation = session([{ ticker: '1234', purpose: 'new' }], [])
    operation.status = 'active'
    expect(render(operation)).toContain('調査進行中')
  })
})
