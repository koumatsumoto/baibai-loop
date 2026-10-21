import { createElement } from 'react'
import { MemoryRouter } from 'react-router'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { OperationResearchTargetView, OperationSessionView } from '../src/api/types'
import { ResearchOperationCard } from '../src/pages/research/ResearchOperationCard'

function target(ticker: string, purpose: 'new' | 'holding', extra: Partial<OperationResearchTargetView> = {}): OperationResearchTargetView {
  return { ticker, purpose, outcome_status: null, assessment_ref: null, unfinished_reason: null, references: [], selection_rationale: null, research_question: null, key_risk: null, ...extra }
}
function session(targets: OperationResearchTargetView[]): OperationSessionView {
  return {
    operation_id: 'research-op-00000000000040008000000000000001', session_kind: 'capital-allocation', status: 'completed',
    as_of: '2026-10-14', ticker: null, started_at: '2026-10-14T18:00:00+09:00', completed_at: '2026-10-14T19:00:00+09:00',
    payload: { checkpoint: 'synthetic proposal complete', result: 'synthetic completion' },
    research: { request: 'synthetic explicit request', request_scope: 'discover_new', selection_summary: '合成選定理由', max_deep_research_targets: 3, targets, proposal_id: null, proposal_draft_sha256: null, source_run_revision_id: null, source_analysis_as_of: null },
  }
}
function render(operation: OperationSessionView) {
  return renderToStaticMarkup(createElement(MemoryRouter, null, createElement(ResearchOperationCard, { operations: [operation] })))
}

describe('有限調査と提案の表示', () => {
  it('開始要求と固定runを表示する', () => {
    const operation = session([target('1234', 'new')])
    operation.research!.source_run_revision_id = 'synthetic-explicit-run'
    operation.research!.source_analysis_as_of = '2026-10-13'
    const html = render(operation)
    for (const text of ['保存した分析基準日', 'synthetic-explicit-run', 'synthetic explicit request', '2026-10-13']) expect(html).toContain(text)
    expect(html).not.toContain('/research-triage')
  })
  it('発行提案がある部分完了と調査未完了を別表示する', () => {
    const operation = session([target('1234', 'new', { outcome_status: 'reviewed', assessment_ref: { assessment_id: 'stock-assessment-original', core_sha256: 'a'.repeat(64), review_id: 'assessment-review-original' } }), target('5678', 'new', { outcome_status: 'unfinished', unfinished_reason: '一次資料の確認未完了' })])
    operation.research!.proposal_id = 'investment-proposal-original'
    const html = render(operation)
    for (const text of ['提案工程完了', '固定対象 2件', '調査未完了: 一次資料の確認未完了', '/stocks/stock-assessments/stock-assessment-original', '/stocks/investment-proposals/investment-proposal-original']) expect(html).toContain(text)
    expect(html).not.toContain('人間確認済み')
  })
  it('0件と全未完了を提案なしとして表示する', () => {
    expect(render(session([]))).toContain('提案なしで終了')
    expect(render(session([target('1234', 'new', { outcome_status: 'unfinished', unfinished_reason: '調査打切り' })]))).toContain('提案なしで終了')
  })
  it('activeを完了として表示しない', () => {
    const operation = session([target('1234', 'new')]); operation.status = 'active'
    expect(render(operation)).toContain('調査進行中')
  })
  it('完成評価だけでは提案の完成にしない', () => {
    const html = render(session([target('1234', 'new', { outcome_status: 'reviewed' })]))
    expect(html).toContain('企業調査終了')
    expect(html).not.toContain('提案工程完了')
    expect(html).toContain('提案・採否・約定は別に確認')
  })
  it('保有も評価の参照と発行提案の参照を独立表示し、本文を再保存しない', () => {
    const operation = session([target('1234', 'holding', { outcome_status: 'reviewed', assessment_ref: { assessment_id: 'stock-assessment-held', core_sha256: 'b'.repeat(64), review_id: 'assessment-review-held' } })])
    operation.session_kind = 'position-review'
    operation.research!.request_scope = 'review_positions'
    operation.research!.proposal_id = 'investment-proposal-holding'
    const html = render(operation)
    for (const text of ['保有の見直し', '/stocks/stock-assessments/stock-assessment-held', '/stocks/investment-proposals/investment-proposal-holding']) expect(html).toContain(text)
    expect(html).not.toContain('holding_proposal')
    expect(html).not.toContain('保存した価格')
  })
  it('正規進捗が無いpayloadから完成を推測しない', () => {
    const operation = session([])
    operation.research = null
    expect(render(operation)).toContain('有限調査の進行記録はまだありません')
    expect(render(operation)).not.toContain('提案工程完了')
  })
})
