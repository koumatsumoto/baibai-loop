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

it('opens only the selected US decision’s seven perspectives, with its source observation time, not the newest report’s view', () => {
  const data: LatestUSProposalView = {
    ...snapshot,
    proposal: { ...current, model_perspectives: [
      { model: 'company_financials', assessment: 'mixed', summary: '最新の利益率と現金回収の評価。', sources: [{ label: '四半期決算資料', observed_at: '2026-11-27T16:00:00Z' }] },
    ] },
    history: { ...snapshot.history, proposals: [{ ...previous, model_perspectives: [
      { model: 'technical_analysis', assessment: 'insufficient', summary: '観測窓が短く、価格帯は市場全体の終値ではない。', sources: [] },
      { model: 'social_trends', assessment: 'not_applicable', summary: 'この制度改正は対象事業には適用されない。', sources: [{ label: '公表済みの制度資料', observed_at: '2026-11-27T15:31:10Z' }] },
    ] }] },
  }
  const selected = combined(data, previousQuery)
  const latest = combined(data, 'proposal=' + current.proposal_id)
  expect(selected).toContain('売買判断の7つの観点')
  expect(selected.indexOf('同期間のUSD現金との比較')).toBeLessThan(selected.indexOf('売買判断の7つの観点'))
  const labels = [...selected.matchAll(/<th[^>]*scope="row"[^>]*>([^<]+)<\/th>/g)].map(match => match[1])
  expect(labels).toEqual(['企業財務', '企業成長', '市場環境', '外部評価', 'テクニカル', '産業動向', '社会動向'])
  const selectedRow = (model: string) => selected.split('data-model="' + model + '"')[1].split('</tr>')[0]
  expect(selectedRow('technical_analysis')).toContain('>情報不足<')
  expect(selectedRow('social_trends')).toContain('>対象外<')
  expect(selectedRow('market_context')).toContain('>未記録<')
  expect(selected).toContain('観測窓が短く')
  expect(selected).toContain('公表済みの制度資料')
  expect(selected).toContain('dateTime="2026-11-27T15:31:10Z"')
  expect(selected).toContain('2026-11-28 00:31')
  expect(selected).not.toContain('最新の利益率')
  expect(latest).toContain('最新の利益率')
  expect(latest).not.toContain('観測窓が短く')
  expect(selected).not.toContain('data-evidence="observation"')
  expect(selected).not.toContain('EPS（1株当たり利益）')
})

it('does not invent seven judgments from IEX or the decision when the saved record has no authored rows', () => {
  const html = combined(snapshot, previousQuery)
  expect(html).toContain('この判断には観点別の評価が保存されていません。')
  expect(html).toContain(previous.reason)
  expect(html).toContain('Alpaca IEXの分割調整済み観測')
  expect(html).not.toContain('data-evidence="author"')
})

it.each(['missing', 'bad-clock', 'duplicate'] as const)('isolates the %s perspective data, keeping the selected core and not borrowing the latest rows', bad => {
  const row = { model: 'social_trends' as const, assessment: 'mixed' as const, summary: '壊れた欄の推測禁止。',
    sources: [{ label: '資料', observed_at: bad === 'bad-clock' ? 'invalid' : '2026-11-27T15:31:10Z' }] }
  const report = { ...previous, model_perspectives: bad === 'duplicate' ? [row, row] : [row] }
  if (bad === 'missing') Reflect.deleteProperty(report, 'model_perspectives')
  const data: LatestUSProposalView = {
    ...snapshot, proposal: { ...current, model_perspectives: [{ ...row, summary: '現在側の評価。', sources: [{ label: '資料', observed_at: '2026-11-27T15:31:10Z' }] }] },
    history: { ...snapshot.history, proposals: [report] },
  }
  const html = combined(data, previousQuery)
  for (const text of [previous.proposal_id, previous.draft_sha256, previous.reason, '同期間のUSD現金との比較', '観点別の評価を読み取れません。']) expect(html).toContain(text)
  for (const text of [current.proposal_id, '現在側の評価。', '壊れた欄の推測禁止。', '観点別の評価が保存されていません。', 'data-evidence="author"']) expect(html).not.toContain(text)
  expect(combined(data, 'proposal=' + current.proposal_id)).toContain('現在側の評価。')
})

it('explains which comparison input is missing on the selected deferred report, without borrowing a close from another report', () => {
  const unknown = { action: 'defer', comparison: null, comparison_horizon_et: null } as const
  const data: LatestUSProposalView = {
    ...snapshot,
    proposal: { ...current, ...unknown, official_close: null },
    history: { ...snapshot.history, proposals: [{ ...previous, ...unknown }] },
  }
  const missingClose = combined(data, 'proposal=' + current.proposal_id)
  const recordedClose = combined(data, previousQuery)
  expect(missingClose).toContain('比較の起点となる公式終値は未確認です。')
  expect(missingClose).not.toContain('公式終値は確認済みです。')
  expect(recordedClose).toContain('公式終値は確認済みです。')
  expect(recordedClose).toContain('基本・下方ケースとUSD現金を同じ終期で比べた結果は、この判断にはありません。')
  expect(recordedClose).not.toContain('比較の起点となる公式終値は未確認です。')
  for (const html of [missingClose, recordedClose]) {
    expect(html).toContain('判断保留')
    expect(html).toContain('比較未確定')
    expect(html).not.toMatch(/>現金との差<|>見送り<|<form|<button/)
  }
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

it.each<[string, Partial<LatestUSProposalView>]>([
  ['no generation clock', { generated_at: undefined }],
  ['invalid generation clock', { generated_at: 'unknown' }],
  ['unqualified generation clock', { generated_at: '2026-11-28T18:00:00' }],
  ['latest error', { error: 'projection failed' }],
  ['history error', { history: { ...fixture.history, status: 'empty', error: 'projection failed' } }],
])('does not claim the App has no saved decisions with %s', (_case, damaged) => {
  const empty: LatestUSProposalView = {
    ...fixture, status: 'empty', proposal: null,
    history: { ...fixture.history, status: 'empty' },
  }
  const data = { ...empty, ...damaged }
  for (const query of ['', previousQuery]) {
    const html = combined(data, query)
    expect(html).toContain('米国株の判断を読み取れません')
    expect(html).not.toContain('保存済みの米国株の判断はありません')
    expect(html).not.toContain('projection failed')
  }
})

it('retains a separately readable latest only on the unselected landing page when history cannot be read', () => {
  for (const data of [
    { ...snapshot, history: { ...fixture.history, status: 'unavailable', has_more: null } } as LatestUSProposalView,
    { ...snapshot, history: { ...fixture.history, status: 'unavailable', has_more: null, unavailable_proposal_ids: [previous.proposal_id] } } as LatestUSProposalView,
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
      const explicitlyUnreadable = query === previousQuery
        && data.history?.unavailable_proposal_ids.includes(previous.proposal_id)
      expect(selected).toContain(explicitlyUnreadable ? 'この保存判断は読み取れません' : '米国株の判断を読み取れません')
      expect(selected).not.toContain(current.reason)
      expect(selected).not.toContain(previous.reason)
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

it.each<[string, LatestUSProposalView['history']]>([
  ['duplicate current', { ...fixture.history, proposals: [current] }],
  ['the same ID with a contradictory decision', { ...fixture.history, proposals: [{
    ...current, action: 'defer', official_close: null, comparison: null, comparison_horizon_et: null,
  }] }],
  ['the same ID on another ticker', { ...fixture.history, proposals: [{ ...current, ticker: 'GOOG' }] }],
  ['a malformed body carrying the current ID', { ...fixture.history, proposals: [{ ...current, published_at: 'unknown' }] }],
  ['the current ID explicitly unavailable', { ...fixture.history, status: 'partial', unavailable_proposal_ids: [current.proposal_id] }],
  ['the current ID explicitly unavailable with no readable history', { ...fixture.history, status: 'unavailable', unavailable_proposal_ids: [current.proposal_id] }],
])('does not resurrect the disputed latest after %s removes the last readable entry', (_case, history) => {
  const data = { ...snapshot, history }
  const landing = combined(data)
  expect(landing).toContain('米国株の判断を読み取れません')
  expect(landing).not.toContain('以下は最新の保存判断です')
  expect(landing).not.toContain(current.proposal_id)
  expect(landing).not.toContain(current.reason)
  const exact = combined(data, 'proposal=' + current.proposal_id)
  expect(exact).toContain('この保存判断は読み取れません')
  expect(exact).not.toContain(current.proposal_id)
  expect(exact).not.toContain(current.reason)
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
