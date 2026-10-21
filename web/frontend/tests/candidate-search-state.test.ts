import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { expect, it } from 'vitest'

import { StockAnalysisSection } from '../src/pages/stocks/StockAnalysisSection'
import { stockAnalysisRow } from './helpers/stock-analysis'

it('candidate filtering reads the same rows and survives route back/forward', async () => {
  const rows = [
    stockAnalysisRow({ ticker: '1111', name: '候補企業' }),
    stockAnalysisRow({ ticker: '2222', name: '候補外企業', candidate: { common_eligible: true, reasons: [], normalized_per_3fy_sector_gap: 0 } }),
  ]
  const router = createMemoryRouter([
    { path: '/candidates', element: createElement(StockAnalysisSection, { rows, runAsOf: '2026-10-14' }) },
  ], { initialEntries: ['/candidates?candidates=1', '/candidates?q=2222'], initialIndex: 1 })
  const render = () => renderToStaticMarkup(createElement(RouterProvider, { router }))
  expect(render()).toContain('候補外企業')
  await router.navigate(-1)
  const candidateOnly = render()
  expect(candidateOnly).toContain('候補企業')
  expect(candidateOnly).not.toContain('候補外企業')
  expect(router.state.location.search).toBe('?candidates=1')
  await router.navigate(1)
  expect(render()).toContain('候補外企業')
  expect(router.state.location.search).toBe('?q=2222')
  expect(rows[1].candidate.normalized_per_3fy_sector_gap).toBe(0)
})
