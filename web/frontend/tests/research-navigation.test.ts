import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import type { CapitalAllocationAssessmentSummaryView } from '../src/api/types'
import { ResearchNav } from '../src/components/ResearchNav'
import { AllocationProposals } from '../src/pages/ResearchPage'

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
    const assessments: CapitalAllocationAssessmentSummaryView[] = [{ capital_allocation_assessment_id: 'synthetic-original-caa', as_of: '2026-09-07', published_at: '2026-09-08T10:00:00+09:00', decision: 'no_allocation', headline: '余力を温存する', research_triage_id: 'synthetic-original-triage', alternative_count: 2, allocated_ticker: null, research_operation_id: null }]
    const html = render(createElement(AllocationProposals, { assessments }))
    for (const text of ['配分見送り', '余力を温存する', '/stocks/capital-allocation-assessments/synthetic-original-caa', '2026-09-07', '採否・注文・約定は別工程']) expect(html).toContain(text)
    expect(html).not.toContain('買い')
  })
})
