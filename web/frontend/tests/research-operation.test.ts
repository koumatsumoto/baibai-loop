import { createElement, Fragment } from 'react'
import { MemoryRouter } from 'react-router'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { OperationResearchTargetView, OperationSessionView } from '../src/api/types'
import { HoldingProposals, ResearchOperationCard } from '../src/pages/research/ResearchOperationCard'

function target(ticker: string, purpose: 'new' | 'holding', extra: Partial<OperationResearchTargetView> = {}): OperationResearchTargetView {
  return { ticker, purpose, outcome_status: null, thesis_id: null, thesis_review_id: null, unfinished_reason: null, references: [], checked_at: null, holding_proposal: null, ...extra }
}
function session(targets: OperationResearchTargetView[]): OperationSessionView {
  return {
    operation_id: 'op-20260907-capital-allocation-1', session_kind: 'capital-allocation', status: 'completed',
    as_of: '2026-09-07', ticker: null, started_at: '2026-09-07T18:00:00+09:00', completed_at: '2026-09-07T18:00:00+09:00',
    payload: { checkpoint: 'synthetic proposal complete', result: 'synthetic completion', human_confirmation: null },
    research: { request: 'synthetic explicit request', research_triage_id: null, targets, capital_allocation_assessment_id: null, source_run_revision_id: null, source_analysis_as_of: null },
  }
}
function render(operation: OperationSessionView) { return renderToStaticMarkup(createElement(MemoryRouter, null, createElement(Fragment, null, createElement(HoldingProposals, { operations: [operation] }), createElement(ResearchOperationCard, { operations: [operation] })))) }

describe('有限調査と提案の表示', () => {
  it('ユーザー指定の要求と固定runを表示し、Triage参照を補わない', () => {
    const operation = session([target('1234', 'new')])
    operation.research!.source_run_revision_id = 'synthetic-explicit-run'
    operation.research!.source_analysis_as_of = '2026-09-07'
    const html = render(operation)
    for (const text of ['ユーザー指定の調査', 'synthetic-explicit-run', 'synthetic explicit request', '2026-09-07']) expect(html).toContain(text)
    expect(html).not.toContain('/research-triage')
  })
  it('部分完了と保有提案を表示し、人間確認済み公開と分ける', () => {
    const html = render(session([target('1234', 'new', { outcome_status: 'reviewed' }), target('5678', 'new', { outcome_status: 'unfinished', unfinished_reason: '一次資料の確認未完了' }), target('2331', 'holding', { outcome_status: 'proposal', holding_proposal: { action: 'hold' }, checked_at: '2026-09-07T18:00:00+09:00' })]))
    for (const text of ['提案工程完了', '固定対象 3件', '調査未完了: 一次資料の確認未完了', '保有提案 hold（人間確認済み公開とは別）', '検証時点 2026-09-07T18:00:00+09:00']) expect(html).toContain(text)
  })
  it('0件と全未完了を提案なしとして表示する', () => {
    expect(render(session([]))).toContain('提案なしで終了')
    expect(render(session([target('1234', 'new', { outcome_status: 'unfinished', unfinished_reason: '調査打切り' })]))).toContain('提案なしで終了')
  })
  it('activeを完了として表示しない', () => {
    const operation = session([target('1234', 'new')]); operation.status = 'active'
    expect(render(operation)).toContain('調査進行中')
  })
  it('Reviewed Thesisだけでは配分提案の完成へ進めない', () => {
    const html = render(session([target('1234', 'new', { outcome_status: 'reviewed' })]))
    expect(html).toContain('企業調査終了')
    expect(html).not.toContain('提案工程完了')
    expect(html).toContain('配分の提案・採否・約定を意味しません')
  })
  it('保有の未確定理由と保存した価格basisを維持する', () => {
    const html = render(session([target('1234', 'holding', { outcome_status: 'proposal', holding_proposal: { action: null, unresolved_reason: '企業評価の確認待ち', quote: { price_yen: 1200, price_basis: 'last_close_unadjusted', observed_at: '2026-09-07T15:30:00+09:00' } } })]))
    for (const text of ['保有判断未確定', '企業評価の確認待ち', 'last_close_unadjusted', '1200円', '最新の銘柄情報']) expect(html).toContain(text)
    expect(html).not.toContain('売却の提案')
  })
  it('保存済みのCAAと指定Triageへ進み、旧payloadから進捗を推測しない', () => {
    const operation = session([])
    operation.research!.capital_allocation_assessment_id = 'synthetic-caa'
    operation.research!.research_triage_id = 'synthetic-triage'
    const html = render(operation)
    expect(html).toContain('/stocks/capital-allocation-assessments/synthetic-caa')
    expect(html).toContain('/research-triage?research_triage_id=synthetic-triage')
    operation.research = null
    expect(render(operation)).toContain('有限調査の進行記録はまだありません')
    expect(render(operation)).not.toContain('提案工程完了')
  })
})
