import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createMemoryRouter, MemoryRouter, RouterProvider, useSearchParams } from 'react-router'
import { expect, it } from 'vitest'

import type { LatestUSProposalView, USProposalView } from '../src/api/types'
import { AppLayout } from '../src/components/AppLayout'
import { USProposalHistory } from '../src/pages/USProposalPage'
import { usProposalFixture as fixture } from './fixtures/us-proposal'

const current: USProposalView = {
  ...fixture.proposal!,
  published_at: '2026-11-28T17:10:00.000002Z',
  session_et: '2026-11-26',
}
const previous: USProposalView = {
  ...current,
  proposal_id: 'us-proposal-saved-previous',
  draft_sha256: 'b'.repeat(64),
  action: 'defer',
  reason: '過去の保存判断。追加資料を待つ。',
  official_close: { price_usd: '100.50', origin: 'alpaca_sip_nasdaq_official_close', observed_at: '2026-11-28T08:15:30.123456Z' },
  published_at: '2026-11-29T02:10:00.000001+09:00',
  session_et: '2026-11-27',
}
const first: USProposalView = {
  ...current,
  proposal_id: 'us-proposal-saved-first',
  ticker: 'BRK.B',
  venue: 'XNYS',
  share_class: 'Class B common',
  published_at: '2026-11-27T11:01:00-05:00',
}
const snapshot: LatestUSProposalView = {
  ...fixture,
  proposal: current,
  history: { ...fixture.history, proposals: [previous, first] },
}
const previousQuery = new URLSearchParams({ proposal: previous.proposal_id, ticker: previous.ticker }).toString()
const previousHref = '/us-proposal?' + previousQuery
const combined = (data: LatestUSProposalView, query = '') => renderToStaticMarkup(createElement(MemoryRouter, null,
  createElement(USProposalHistory, { state: { kind: 'loaded', data }, search: new URLSearchParams(query) }),
))

it('restores the top-level current exactly once; keeps publication order, identical tickers, ET dates and USD units', () => {
  expect(combined(fixture)).toContain('1件のレポート')
  const html = combined(snapshot)
  expect(html).toContain('3件のレポート')
  expect(html.match(/GOOGL · XNAS · USD/g)).toHaveLength(2)
  expect(html).toContain('BRK.B · XNYS · USD')
  expect(html.match(/>最新の保存判断</g)).toHaveLength(1)
  expect(html).toContain('見送り')
  expect(html).toContain('判断保留')
  expect(html.indexOf(current.proposal_id)).toBeLessThan(html.indexOf(previous.proposal_id))
  expect(html.indexOf(previous.proposal_id)).toBeLessThan(html.indexOf(first.proposal_id))
  expect(html).toContain('対象立会日（米東部時間）：')
  expect(html).toContain('2026-11-26</time>')
  expect(html).toContain('2026-11-27</time>')
  expect(html).toContain('2026-11-29 02:10')
  expect(html).toContain('dateTime="' + current.published_at + '"')
  expect(html).toContain('dateTime="' + previous.published_at + '"')
  expect(html).toContain('表示データ生成（JST）')
  expect(html).not.toMatch(/JPY|<form|<button/)
})

it('shows the exact older owner with its SIP source, saved ID, SHA and a list back link, never the latest body', () => {
  const html = combined(snapshot, previousQuery)
  expect(html).toContain('href="/us-proposal"')
  expect(html).toContain('米国株のレポート一覧')
  expect(html).toContain(previous.proposal_id)
  expect(html).toContain(previous.draft_sha256)
  expect(html).toContain(previous.reason)
  expect(html).toContain('Nasdaqの無調整公式終値（Alpaca SIP経由）')
  expect(html).toContain('終値データの受領時刻（JST）')
  expect(html).toContain('2026-11-28T08:15:30.123456Z')
  expect(html).toContain('同期間のUSD現金との比較')
  expect(html).not.toContain(current.proposal_id)
  expect(html).not.toContain(current.reason)
  expect(combined(snapshot, 'proposal=' + previous.proposal_id)).toContain(previous.draft_sha256)
})

it.each([
  ['out of this window', 'proposal=us-proposal-unlisted', 'この表示範囲にありません'],
  ['wrong exact ticker', 'proposal=' + previous.proposal_id + '&ticker=GOOG', 'この表示範囲にありません'],
  ['ticker alias', 'proposal=' + first.proposal_id + '&ticker=BRK-B', 'この表示範囲にありません'],
  ['wrong market publication', 'proposal=investment-proposal-00000000-0000-4000-8000-000000000003', 'この表示範囲にありません'],
  ['ticker only', 'ticker=GOOGL', '判断IDと銘柄を確認して'],
  ['blank ID', 'proposal=&ticker=GOOGL', '判断IDと銘柄を確認して'],
  ['blank ticker', 'proposal=' + previous.proposal_id + '&ticker=', '判断IDと銘柄を確認して'],
  ['duplicate ID', 'proposal=' + current.proposal_id + '&proposal=' + previous.proposal_id, '判断IDと銘柄を確認して'],
  ['duplicate ticker', previousQuery + '&ticker=GOOG', '判断IDと銘柄を確認して'],
])('does not replace %s with another publication', (_why, query, message) => {
  const html = combined({ ...snapshot, history: { ...snapshot.history, has_more: true } }, query)
  expect(html).toContain(message)
  expect(html).toContain('米国株のレポート一覧')
  expect(html).not.toContain(current.reason)
  expect(html).not.toContain(previous.reason)
  expect(html).not.toContain('保存済みの米国株の判断はありません')
})

it('uses other readable history while the newest owner is unreadable, only on an exact request', () => {
  const data: LatestUSProposalView = {
    ...snapshot, status: 'unavailable', error: 'synthetic private DB location', proposal: null,
    history: { ...snapshot.history, status: 'partial', error: 'synthetic private ID failure', unavailable_proposal_ids: [current.proposal_id] },
  }
  const list = combined(data)
  expect(list).toContain('2件のレポート')
  expect(list).toContain('最新の保存判断を読み取れません')
  expect(list).toContain('一部の保存判断を読み取れません')
  expect(list.match(/>最新の保存判断</g)).toBeNull()
  expect(list).not.toContain(current.proposal_id)
  expect(list).not.toContain('synthetic private')
  expect(combined(data, previousQuery)).toContain(previous.reason)
  const missing = combined(data, 'proposal=' + current.proposal_id)
  expect(missing).toContain('この保存判断は読み取れません')
  expect(missing).not.toContain(previous.reason)
})

it('mentions partial unknown errors and bounded windows without inventing unavailable IDs or a missing record', () => {
  const html = combined({
    ...snapshot,
    history: { ...snapshot.history, status: 'partial', has_more: true, unavailable_proposal_ids: [] },
  })
  expect(html).toContain('一部の保存判断を読み取れません')
  expect(html).toContain('一覧には直近の発行分だけを表示しています')
  expect(html).toContain('3件のレポート')
  expect(html).not.toContain('最新の保存判断を読み取れません')
})

it('distinguishes a real zero-row App from missing, malformed, or unavailable history', () => {
  const empty: LatestUSProposalView = {
    ...fixture, status: 'empty', proposal: null,
    history: { ...fixture.history, status: 'empty' },
  }
  expect(combined(empty)).toContain('保存済みの米国株の判断はありません')
  expect(combined(empty, previousQuery)).toContain('この表示範囲にありません')
  for (const data of [
    { ...empty, history: { ...empty.history, status: 'unavailable', has_more: null } } as LatestUSProposalView,
    { ...empty, history: undefined } as unknown as LatestUSProposalView,
    { ...empty, history: { ...snapshot.history } },
  ]) for (const query of ['', 'proposal=' + current.proposal_id]) {
    const html = combined(data, query)
    expect(html).toContain('米国株の判断を読み取れません')
    expect(html).not.toContain('保存済みの米国株の判断はありません')
    expect(html).not.toContain(current.reason)
  }
})

it('retains a separately readable latest only on the unselected landing page when history cannot be read', () => {
  for (const data of [
    { ...snapshot, history: { ...fixture.history, status: 'unavailable', has_more: null } } as LatestUSProposalView,
    { ...snapshot, history: undefined } as unknown as LatestUSProposalView,
    { ...snapshot, history: { ...fixture.history, status: 'empty' } } as LatestUSProposalView,
  ]) {
    const landing = combined(data)
    expect(landing).toContain('履歴を読み取れません。以下は最新の保存判断です。')
    expect(landing).toContain(current.reason)
    expect(landing).toContain(current.draft_sha256)
    expect(landing).not.toMatch(/件のレポート|保存済みの米国株の判断はありません/)
    for (const query of [previousQuery, 'proposal=' + current.proposal_id]) {
      const selected = combined(data, query)
      expect(selected).toContain('米国株の判断を読み取れません')
      expect(selected).not.toContain(current.reason)
      expect(selected).not.toContain('以下は最新の保存判断です')
    }
    const invalidCurrent = combined({ ...data, proposal: { ...current, published_at: 'unknown' } })
    expect(invalidCurrent).toContain('米国株の判断を読み取れません')
    expect(invalidCurrent).not.toContain('以下は最新の保存判断です')
  }
})

it('reports a known exact unavailable selection even when there are no readable history rows', () => {
  const data: LatestUSProposalView = {
    ...snapshot, status: 'unavailable', proposal: null,
    history: { ...fixture.history, status: 'unavailable', has_more: false, unavailable_proposal_ids: [current.proposal_id] },
  }
  const selected = combined(data, 'proposal=' + current.proposal_id)
  expect(selected).toContain('この保存判断は読み取れません。別の判断では代用していません。')
  expect(selected).not.toContain(current.proposal_id)
  expect(selected).not.toContain('この表示範囲にありません')
  expect(combined(data)).toContain('米国株の判断を読み取れません')
  expect(combined(data, previousQuery)).toContain('米国株の判断を読み取れません')
})

it('keeps independently readable siblings and refuses an ambiguous or malformed exact row', () => {
  const dupe: LatestUSProposalView = {
    ...snapshot, history: { ...snapshot.history, proposals: [previous, current, { ...first, published_at: 'unknown' }] },
  }
  const html = combined(dupe)
  expect(html).toContain('1件のレポート')
  expect(html).toContain(previous.proposal_id)
  expect(html).not.toContain(current.proposal_id)
  expect(html).not.toContain(first.proposal_id)
  expect(combined(dupe, 'proposal=' + current.proposal_id)).toContain('この保存判断は読み取れません')
  expect(combined(dupe, 'proposal=' + first.proposal_id)).toContain('この保存判断は読み取れません')
  expect(combined(dupe, previousQuery)).toContain(previous.reason)
})

it('returns to the exact saved US report on Back/Forward under the existing market tabs', async () => {
  function USRoute() {
    const [search] = useSearchParams()
    return createElement(USProposalHistory, { state: { kind: 'loaded', data: snapshot }, search })
  }
  const router = createMemoryRouter([{ element: createElement(AppLayout), children: [
    { path: '/us-proposal', element: createElement(USRoute) },
    { path: '/proposal', element: createElement('main', null, '日本株の履歴') },
  ] }], { initialEntries: ['/us-proposal'] })
  const render = () => renderToStaticMarkup(createElement(RouterProvider, { router }))
  const exactLink = 'href="' + previousHref.replace('&', '&amp;') + '"'
  expect(render()).toContain(exactLink)
  await router.navigate(previousHref)
  expect(render()).toContain(previous.reason)
  expect(render()).toContain('米国株のレポート一覧')
  await router.navigate('/proposal')
  expect(render()).toContain('日本株の履歴')
  await router.navigate(-1)
  expect(router.state.location.pathname + router.state.location.search).toBe(previousHref)
  expect(render()).toContain(previous.draft_sha256)
  await router.navigate(-1)
  expect(render()).toContain('3件のレポート')
  await router.navigate(1)
  expect(render()).toContain(previous.reason)
  await router.navigate('/us-proposal')
  expect(render()).toContain(exactLink)
})
