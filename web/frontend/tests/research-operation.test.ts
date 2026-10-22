import { createElement } from 'react'
import { MemoryRouter } from 'react-router'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { OperationResearchTargetView, OperationSessionView } from '../src/api/types'
import { ResearchOperationCard } from '../src/pages/research/ResearchOperationCard'

function target(ticker: string, purpose: 'new' | 'holding', extra: Partial<OperationResearchTargetView> = {}): OperationResearchTargetView {
  return { ticker, purpose, outcome_status: null, assessment_ref: null, unfinished_reason: null, references: extra.outcome_status === 'unfinished' ? ['synthetic-source'] : [], selection_rationale: null, research_question: null, key_risk: null, ...extra }
}
function session(targets: OperationResearchTargetView[], status: 'active' | 'completed' = 'active'): OperationSessionView {
  const proposed = status === 'completed' && targets.length > 0
  return {
    operation_id: 'research-op-00000000000040008000000000000001', session_kind: 'capital-allocation', status,
    as_of: '2026-10-14', ticker: null, started_at: '2026-10-14T18:00:00+09:00', completed_at: status === 'completed' ? '2026-10-14T19:00:00+09:00' : null,
    payload: { checkpoint: 'synthetic research', ...(status === 'completed' ? { result: 'synthetic completion', next: '次の明示依頼で再確認' } : {}) },
    research: { request: 'synthetic explicit request', request_scope: 'discover_new', selection_summary: '合成選定理由', max_deep_research_targets: 3, targets, proposal_id: proposed ? 'investment-proposal-original' : null, proposal_draft_sha256: proposed ? 'c'.repeat(64) : null, source_run_revision_id: null, source_analysis_as_of: null },
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
    expect(html).toContain('企業評価・Review完了 0件 · 調査未完了 0件 · 成果未記録 1件')
    expect(html).not.toContain('/research-triage')
  })
  it('発行提案がある部分完了と調査未完了を別表示する', () => {
    const operation = session([target('1234', 'new', { outcome_status: 'reviewed', assessment_ref: { assessment_id: 'stock-assessment-original', core_sha256: 'a'.repeat(64), review_id: 'assessment-review-original' } }), target('5678', 'new', { outcome_status: 'unfinished', unfinished_reason: '一次資料の確認未完了' })], 'completed')
    const html = render(operation)
    for (const text of ['>調査終了</p>', '固定対象 2件', '企業評価・Review完了 1件 · 調査未完了 1件 · 成果未記録 0件', '保存提案あり', '調査未完了: 一次資料の確認未完了', '/stocks/stock-assessments/stock-assessment-original', '/stocks/investment-proposals/investment-proposal-original']) expect(html).toContain(text)
    expect(html).not.toContain('提案工程完了')
    expect(html).not.toContain('人間確認済み')
  })
  it('0対象の理由ある終了を全対象未完了や提案ありにしない', () => {
    const html = render(session([], 'completed'))
    for (const text of ['>調査終了</p>', '固定対象 0件', '今回の調査対象は0件', '合成選定理由', '保存提案の参照はありません']) expect(html).toContain(text)
    expect(html).not.toContain('保存提案あり')
    expect(html).not.toContain('/stocks/investment-proposals/')
  })
  it('全対象unfinishedを保存defer提案の参照とともに表示し、見送りや提案完成にしない', () => {
    const operation = session([target('1234', 'new', { outcome_status: 'unfinished', unfinished_reason: '一次資料の確認待ち', references: ['synthetic-source'] })], 'completed')
    operation.payload.result = '全対象未完了。保存提案のdeferに再開条件を記録。'
    const html = render(operation)
    for (const text of ['>調査終了</p>', '企業評価・Review完了 0件 · 調査未完了 1件 · 成果未記録 0件', '調査未完了: 一次資料の確認待ち', '保存提案あり', '保存提案のdefer', '次の確認: 次の明示依頼で再確認', '/stocks/investment-proposals/investment-proposal-original']) expect(html).toContain(text)
    expect(html).not.toContain('提案工程完了')
    expect(html).not.toContain('提案なしで終了')
  })
  it('activeは全対象を被覆する保存提案があっても終了にしない', () => {
    const operation = session([target('1234', 'new', { outcome_status: 'unfinished', unfinished_reason: '再開可能' })])
    operation.research!.proposal_id = 'investment-proposal-earlier'
    const html = render(operation)
    for (const text of ['>調査進行中</p>', '保存提案あり', '企業評価・Review完了 0件 · 調査未完了 1件 · 成果未記録 0件', '/stocks/investment-proposals/investment-proposal-earlier']) expect(html).toContain(text)
    expect(html).not.toContain('>調査終了</p>')
    expect(html).not.toContain('提案工程完了')
  })
  it('完成評価だけでは提案やOperationを完了にしない', () => {
    const html = render(session([target('1234', 'new', { outcome_status: 'reviewed', assessment_ref: { assessment_id: 'stock-assessment-original', core_sha256: 'a'.repeat(64), review_id: 'assessment-review-original' } })]))
    expect(html).toContain('>調査進行中</p>')
    expect(html).toContain('企業評価・Review完了 1件 · 調査未完了 0件 · 成果未記録 0件')
    expect(html).toContain('保存提案の参照はありません')
    expect(html).not.toContain('提案工程完了')
    expect(html).toContain('提案・採否・約定は別に確認')
  })
  it('保有の終了と提案参照から具体売却条件の完成を推定しない', () => {
    const operation = session([target('1234', 'holding', { outcome_status: 'reviewed', assessment_ref: { assessment_id: 'stock-assessment-held', core_sha256: 'b'.repeat(64), review_id: 'assessment-review-held' } })], 'completed')
    operation.session_kind = 'position-review'
    operation.research!.request_scope = 'review_positions'
    operation.research!.proposal_id = 'investment-proposal-holding'
    operation.payload.result = '売却判断を保存。quote不足で具体条件は未確定。'
    const html = render(operation)
    for (const text of ['>調査終了</p>', '保存提案あり', '保有の見直し', '具体条件は未確定', '/stocks/stock-assessments/stock-assessment-held', '/stocks/investment-proposals/investment-proposal-holding']) expect(html).toContain(text)
    expect(html).not.toContain('提案工程完了')
    expect(html).not.toContain('holding_proposal')
    expect(html).not.toContain('保存した価格')
  })
  it.each(['active', 'completed'] as const)('固定対象情報のない%s記録の閲覧を保ち、本文から成果を推測しない', status => {
    const operation = session([], status)
    operation.research = null
    operation.session_kind = 'position-review'
    operation.ticker = '1234'
    operation.payload.result = '提案工程完了'
    const html = render(operation)
    for (const text of ['固定対象情報のない保存記録', '保存状態:', status === 'active' ? '進行中' : '終了', '保有の見直し', '1234', 'research-op-00000000000040008000000000000001', '開始', '対象数・成果の完成は判定しません']) expect(html).toContain(text)
    expect(html).not.toContain('進行記録はまだありません')
    expect(html).not.toContain('固定対象 0件')
    expect(html).not.toContain('保存提案あり')
    expect(html).not.toContain('提案工程完了')
  })
})
