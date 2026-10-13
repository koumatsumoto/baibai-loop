import { createElement, Fragment } from 'react'
import { MemoryRouter } from 'react-router'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { OperationSessionView } from '../src/api/types'
import { HoldingProposals, ResearchOperationCard } from '../src/pages/research/ResearchOperationCard'

function session(targets: object[], outcomes: object[]): OperationSessionView {
  return {
    operation_id: 'op-20260907-capital-allocation-1', session_kind: 'capital-allocation', status: 'completed',
    as_of: '2026-09-07', ticker: null, started_at: '2026-09-07T18:00:00+09:00', completed_at: '2026-09-07T18:00:00+09:00',
    payload: { checkpoint: 'synthetic proposal complete', artifacts: [{ kind: 'research_selection', request: 'synthetic explicit request', targets }, { kind: 'research_progress', outcomes }], result: 'synthetic completion', human_confirmation: null },
  }
}
function render(operation: OperationSessionView) { return renderToStaticMarkup(createElement(MemoryRouter, null, createElement(Fragment, null, createElement(HoldingProposals, { operations: [operation] }), createElement(ResearchOperationCard, { operations: [operation] })))) }

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

describe('提案と企業評価の区別', () => {
  it('Reviewed Thesisだけでは配分提案の完成へ進めない', () => {
    const html = render(session([{ ticker: '1234', purpose: 'new' }], [{ ticker: '1234', status: 'reviewed' }]))
    expect(html).toContain('企業調査終了')
    expect(html).not.toContain('提案工程完了')
    expect(html).toContain('配分の提案・採否・約定を意味しません')
  })
  it('保有の未確定理由と保存した価格basisを維持する', () => {
    const html = render(session([{ ticker: '1234', purpose: 'holding' }], [{ ticker: '1234', status: 'proposal', draft: { action: null, unresolved_reason: '企業評価の確認待ち', quote: { price_yen: 1200, price_basis: 'last_close_unadjusted', observed_at: '2026-09-07T15:30:00+09:00' } } }]))
    for (const text of ['保有判断未確定', '企業評価の確認待ち', 'last_close_unadjusted', '1200円', '最新の銘柄情報']) expect(html).toContain(text)
    expect(html).not.toContain('売却の提案')
  })
})
