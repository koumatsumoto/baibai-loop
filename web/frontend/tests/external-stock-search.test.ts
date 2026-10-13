import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'

import type { ExternalAssessmentSecurityView } from '../src/api/types'
import { filterSecurityRows, projectSecurityRows, type SecurityFilters } from '../src/lib/stocks'
import { SecurityAnalysisSection } from '../src/pages/stocks/SecurityAnalysisSection'

const identities: ExternalAssessmentSecurityView[] = [
  { ticker: '9999', name: '合成検索企業', sector_33: 'サービス業', portfolio_state: 'unheld', has_research: false },
  { ticker: '123A', name: null, sector_33: null, portfolio_state: 'held', has_research: true },
]
const filters: SecurityFilters = { query: '', sector: 'all', perMax: null, pbrMax: null, dividendMinPct: null, heldOnly: false, researchOnly: false }

describe('保存済み外部観測の既存銘柄検索', () => {
  it('名称検索、ticker前方一致、全角英数字と既存保有条件を使える', () => {
    for (const [query, ticker] of [['検索', '9999'], ['９９', '9999'], ['１２３ａ', '123A']]) {
      expect(filterSecurityRows(identities, { ...filters, query }).map(row => row.ticker)).toEqual([ticker])
    }
    expect(filterSecurityRows(identities, { ...filters, heldOnly: true, researchOnly: true }).map(row => row.ticker)).toEqual(['123A'])
    expect(projectSecurityRows(identities, filters, { key: 'er_annual', direction: 'desc' }, 100).page).toBe(1)
  })
  it('未評価の指標を数値filterへ通さない', () => {
    for (const condition of [{ perMax: 10 }, { pbrMax: 1 }, { dividendMinPct: 0 }]) expect(filterSecurityRows(identities, { ...filters, ...condition })).toEqual([])
  })
  it('run未発行でも詳細linkを出し、名称不明と機械分析なしを明示する', () => {
    const html = renderToStaticMarkup(createElement(MemoryRouter, null, createElement(SecurityAnalysisSection, { rows: identities, runAsOf: null, externalCutoff: '2026-10-13T00:00:00Z' })))
    for (const text of ['href="/securities/9999"', 'href="/securities/123A"', '合成検索企業', '名称未確認', '機械分析なし', '保存済み外部参考評価', '機械分析の基準 未発行', 'JST']) expect(html).toContain(text)
    expect(html).not.toContain('0.0%')
  })
})
