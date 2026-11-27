import { createElement, lazy, type ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createMemoryRouter, MemoryRouter, Route, RouterProvider, Routes, useSearchParams } from 'react-router'
import { expect, it, vi } from 'vitest'

import type { ProposalHistoryView } from '../src/api/types'
import { AppLayout } from '../src/components/AppLayout'
import { RouteErrorBoundary } from '../src/components/RouteErrorBoundary'
import { proposalReportHref } from '../src/lib/proposal-history'
import { ProposalHistory, ProposalPage } from '../src/pages/ProposalHistoryPage'
import { USProposal, USProposalPage } from '../src/pages/USProposalPage'
import { newProposalFixture } from './fixtures/proposals'

const generatedAt = '2026-11-20T00:00:00Z'
const jpEmpty: ProposalHistoryView = {
  generated_at: generatedAt, status: 'empty', error: null, proposals: [], unavailable_proposal_ids: [],
}
const usPending = createElement(USProposal, { state: { kind: 'pending' } })

function marketNavigation(html: string) {
  return html.match(/<nav\b[^>]*aria-label="売買提案の市場"[^>]*>[\s\S]*?<\/nav>/)?.[0] ?? ''
}

function links(html: string) {
  return Array.from(marketNavigation(html).matchAll(/<a\b([^>]*)>([^<]+)<\/a>/g), ([, attributes, label]) => ({
    attributes, label, href: attributes.match(/href="([^"]*)"/)?.[1],
  }))
}

function expectMarket(html: string, activeHref: string) {
  const market = links(html)
  expect(html.match(/<main\b/g)).toHaveLength(1)
  expect(html.match(/<nav\b/g)).toHaveLength(2)
  expect(market.map(({ label, href }) => [label, href])).toEqual([['日本株', '/proposal'], ['米国株', '/us-proposal']])
  expect(market.filter(({ attributes }) => attributes.includes('aria-current="page"')).map(({ href }) => href))
    .toEqual([activeHref])
  for (const { attributes } of market) {
    expect(attributes).toContain('route-choice')
    expect(attributes).not.toMatch(/underline|bg-primary|before:/)
  }
}

function renderMarket(path: string, page: ReactNode, entry = path) {
  return renderToStaticMarkup(createElement(MemoryRouter, { initialEntries: [entry] },
    createElement(Routes, null, createElement(Route, { element: createElement(AppLayout) },
      createElement(Route, { path, element: page }),
    )),
  ))
}

it.each<[string, ReactNode, string]>([
  ['/proposal', createElement(ProposalPage), '日本株の提案履歴を読み込んでいます'],
  ['/proposal', createElement(ProposalHistory, { data: jpEmpty }), '日本株の保存済みレポートはありません'],
  ['/proposal', createElement(ProposalHistory, { data: { ...jpEmpty, status: 'unavailable', error: '合成の読取失敗' } }), '合成の読取失敗'],
  ['/us-proposal', createElement(USProposalPage), '米国株の判断を読み込んでいます'],
  ['/us-proposal', usPending, '米国株の判断は配信準備中です'],
  ['/us-proposal', createElement(USProposal, { state: { kind: 'unavailable' } }), '米国株の判断を読み取れません'],
])('keeps market navigation on %s through %s', (path, content, label) => {
  const html = renderMarket(path, content, `${path}/`)
  expectMarket(html, path)
  expect(html).toContain(label)
  const navigation = marketNavigation(html)
  expect(navigation).toContain('grid-cols-2')
  expect(navigation).not.toMatch(/role="tab(list)?"/)
  for (const { attributes } of links(html)) {
    expect(attributes).toContain('min-h-11')
    expect(attributes).toContain('focus-visible:outline-2')
    expect(attributes).not.toMatch(/tabIndex=|role=/i)
  }
})

it('keeps market navigation above lazy fallback and the failed route boundary', () => {
  const Pending = lazy(() => new Promise<{ default: () => ReactNode }>(() => {}))
  const pending = renderMarket('/us-proposal', createElement(Pending))
  expectMarket(pending, '/us-proposal')
  expect(pending).toContain('Baibai Loop を読み込んでいます')

  const render = RouteErrorBoundary.prototype.render
  const failed = vi.spyOn(RouteErrorBoundary.prototype, 'render').mockImplementation(function (this: RouteErrorBoundary) {
    this.state = RouteErrorBoundary.getDerivedStateFromError()
    return render.call(this)
  })
  try {
    const unavailable = renderMarket('/proposal', createElement(ProposalHistory, { data: jpEmpty }))
    expectMarket(unavailable, '/proposal')
    expect(unavailable).toContain('この画面を表示できませんでした')
    expect(unavailable).not.toContain('日本株の保存済みレポートはありません')
  } finally {
    failed.mockRestore()
  }
})

it('switches via the market links and restores the exact JP report on Back', async () => {
  const saved = newProposalFixture
  const item = saved.items[0]
  const deepURL = `${proposalReportHref(saved.proposal_id, item.ticker)}#summary`
  function JPReport() {
    const [params] = useSearchParams()
    return createElement(ProposalHistory, {
      data: { ...jpEmpty, status: 'ready', proposals: [saved] },
      proposalId: params.get('proposal'), ticker: params.get('ticker'),
    })
  }
  const router = createMemoryRouter([{ element: createElement(AppLayout), children: [
    { path: '/proposal', element: createElement(JPReport) },
    { path: '/us-proposal', element: usPending },
    { path: '*', element: createElement('main', null, 'ページが見つかりません。') },
  ] }], { initialEntries: [deepURL] })
  const render = () => renderToStaticMarkup(createElement(RouterProvider, { router }))
  const initial = render()
  expectMarket(initial, '/proposal')
  expect(initial).toContain('data-report-summary')
  expect(initial).not.toContain('米国株の判断は配信準備中です')

  await router.navigate(links(initial)[1].href!)
  const us = render()
  expectMarket(us, '/us-proposal')
  expect(us).toContain('米国株の判断は配信準備中です')
  expect(us).not.toContain('data-report-summary')
  await router.navigate(-1)
  const { pathname, search, hash } = router.state.location
  expect(`${pathname}${search}${hash}`).toBe(deepURL)
  expectMarket(render(), '/proposal')
  expect(render()).toContain('data-report-summary')
  await router.navigate(1)
  expectMarket(render(), '/us-proposal')
  await router.navigate('/us-proposal/unknown')
  expect(render()).toContain('ページが見つかりません。')
  expect(marketNavigation(render())).toBe('')
})
