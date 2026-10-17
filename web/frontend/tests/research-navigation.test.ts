import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import type { InvestmentProposalSummaryView } from '../src/api/types'
import { ResearchNav } from '../src/components/ResearchNav'
import { InvestmentProposals } from '../src/pages/ResearchPage'

function render(node: ReturnType<typeof createElement>, path = '/stocks') {
  return renderToStaticMarkup(createElement(MemoryRouter, { initialEntries: [path] }, node))
}
describe('調査・提案の入口', () => {
  it.each([['/stocks', '提案'], ['/stocks/explore', '候補探索'], ['/market-context', '市場環境']])('目的別の二次入口を選択する: %s', (path, label) => {
    const html = render(createElement(ResearchNav), path)
    expect(html.match(/aria-current="page"/g)).toHaveLength(1)
    expect(html).toMatch(new RegExp(`aria-current="page"[^>]*>${label}</a>`))
  })
  it('見送りを買いにせず、保存IDの詳細へ進む', () => {
    const proposals: InvestmentProposalSummaryView[] = [{ proposal_id: 'synthetic-original-proposal', as_of: '2026-10-14', published_at: '2026-10-14T18:00:00+09:00', request_scope: 'specified_stocks', headline: '余力を温存する', item_count: 2, tickers: ['1234', '5678'], actions: ['no_action'], research_operation_id: 'research-op-original' }]
    const html = render(createElement(InvestmentProposals, { proposals }))
    for (const text of ['見送り', '余力を温存する', '/stocks/investment-proposals/synthetic-original-proposal', '2026-10-14', '採否・注文・約定は別工程']) expect(html).toContain(text)
    expect(html).not.toContain('買い')
  })
})

it('複数銘柄に同じ行動を提案しても種類のラベルは重複しない', () => {
  const proposals: InvestmentProposalSummaryView[] = [{ proposal_id: 'synthetic-multiple-buys', as_of: '2026-10-14', published_at: '2026-10-14T18:00:00+09:00', request_scope: 'specified_stocks', headline: '複数銘柄の比較', item_count: 3, tickers: ['1234', '5678', '9012'], actions: ['buy', 'buy', 'defer'], research_operation_id: 'research-op-original' }]
  const html = render(createElement(InvestmentProposals, { proposals }))
  expect(html.match(/購入の提案/g)).toHaveLength(1)
  expect(html.match(/判断保留/g)).toHaveLength(1)
  expect(html).toContain('対象 3件')
})
