import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createMemoryRouter, MemoryRouter, RouterProvider, useSearchParams } from 'react-router'
import { describe, expect, it } from 'vitest'

import type { ProposalHistoryView, SavedProposalView } from '../src/api/types'
import { groupProposalReports, proposalReportHref, publicationPeriod, reportListSummary } from '../src/lib/proposal-history'
import { ProposalHistory } from '../src/pages/ProposalHistoryPage'
import { buyFixture, holdFixture, newProposalFixture, partialBuyFixture, reviewedDeferFixture, sellFixture } from './fixtures/proposals'

function view(proposals: SavedProposalView[], missing: string[] = []): ProposalHistoryView {
  return { generated_at: '2026-11-20T00:00:00Z', status: missing.length ? 'partial' : (proposals.length ? 'ready' : 'empty'), error: missing.length ? '一部を読めません' : null, proposals, unavailable_proposal_ids: missing }
}
function render(data: ProposalHistoryView | null, proposalId: string | null = null, ticker: string | null = null) {
  return renderToStaticMarkup(createElement(MemoryRouter, null, createElement(ProposalHistory, { data, proposalId, ticker })))
}
const newer: SavedProposalView = { ...newProposalFixture, proposal_id: 'proposal-new', as_of: '2026-11-12', published_at: '2026-11-14T03:00:00Z', items: [{ ...reviewedDeferFixture, ticker: '1111', rationale: '新しい保留判断。借換条件の確認が必要。' }] }
const older: SavedProposalView = { ...newProposalFixture, proposal_id: 'proposal-old', as_of: '2026-11-13', published_at: '2026-11-14T02:59:59.999999Z', items: [buyFixture, reviewedDeferFixture] }

describe('publication groups', () => {
  it.each([
    ['2026-11-13T15:00:00Z', '11/14 午前', '2026', '00:00'],
    ['2026-11-14T02:59:59Z', '11/14 午前', '2026', '11:59'],
    ['2026-11-14T03:00:00Z', '11/14 午後', '2026', '12:00'],
    ['2026-12-31T15:00:00Z', '1/1 午前', '2027', '00:00'],
    ['2026-11-14T12:00:00+09:00', '11/14 午後', '2026', '12:00'],
  ])('uses explicit JST publication time at %s', (instant, label, year, time) => {
    expect(publicationPeriod(instant)).toMatchObject({ label, year, time })
  })

  it('keeps server instant ordering and every ticker/report pair, not newest as_of or ticker-only dedupe', () => {
    const groups = groupProposalReports([newer, older])
    expect(groups.map(group => group.label)).toEqual(['11/14 午後', '11/14 午前'])
    expect(groups.flatMap(group => group.reports.map(row => [row.proposal.proposal_id, row.item.ticker]))).toEqual([
      ['proposal-new', '1111'], ['proposal-old', '1111'], ['proposal-old', '3333'],
    ])
  })
})

describe('stock report list and selection', () => {
  it('lists company/code/sector/judgment and the saved summary with stable exact links', () => {
    const html = render(view([newer, older]))
    expect(html).toContain('3件のレポート')
    expect(html).toContain('合成会社')
    expect(html).toContain('情報・通信業')
    expect(html).toContain('判断保留')
    expect(html).toContain('買付（指値）')
    expect(html).toContain('新しい保留判断。借換条件の確認が必要。')
    expect(html).toContain('href="/proposal?proposal=proposal-new&amp;ticker=1111"')
    expect(html).toContain('href="/proposal?proposal=proposal-old&amp;ticker=1111"')
    expect(html.indexOf('11/14 午後')).toBeLessThan(html.indexOf('11/14 午前'))
    expect(html).toContain('発行日時（JST）の新しい順')
    expect(html).toContain('提案基準日 2026-11-12')
    expect(html).toContain('2026年')
  })

  it('selects one ticker from a legacy multi-stock proposal and preserves whole-proposal context in details', () => {
    const html = render(view([newer, older]), older.proposal_id, '1111')
    expect(html).toContain(buyFixture.rationale)
    expect(html).not.toContain(reviewedDeferFixture.rationale)
    expect(html).not.toContain(newer.items[0].rationale)
    expect(html).toContain(older.headline)
    expect(html).toContain(older.comparison)
    expect(html).toContain('この銘柄を含む保存提案のサマリ')
    expect(html).toContain('提案本文の準備時刻')
    expect(html).toContain(`dateTime="${older.prepared_at}"`)
    expect(html).toContain('href="/proposal"')
    expect(html.indexOf('data-report-summary')).toBeLessThan(html.indexOf('7モデルの評価チャート'))
    expect(html.indexOf('7モデルの評価チャート')).toBeLessThan(html.indexOf('data-proposal-details'))
    expect(html).not.toMatch(/<details[^>]*\bopen(?:=|\s|>)/)
  })

  it('does not replace missing/failed selections with a different report', () => {
    const data = view([newer], ['unreadable'])
    const missing = render(data, older.proposal_id, '1111')
    expect(missing).toContain('組合せは、この表示データにありません')
    expect(missing).not.toContain(newer.items[0].rationale)
    expect(render(data, 'unreadable', '1111')).toContain('この提案は読取不能')
    expect(render(data, newer.proposal_id, null)).toContain('レポートを表示できません')
    expect(render(data, null, '1111')).toContain('レポートを表示できません')
    expect(render(data, newer.proposal_id, '9999')).toContain('レポートを表示できません')
  })

  it('shows missing identity and unreadable publication counts, not empty/zero or guessed labels', () => {
    const missing = { ...older, items: [{ ...buyFixture, company_name: null, sector: null }] }
    const html = render(view([missing], ['broken-a', 'broken-b']))
    expect(html).toContain('会社名未記録')
    expect(html).toContain('業種未記録')
    expect(html).toContain('読取不能の提案が2件')
    expect(html).toContain('その提案に含まれる銘柄は一覧に表示できていません')
    expect(html).not.toContain('保存済みレポートはありません')
  })

  it('keeps important late caveats and escapes private prose rather than mechanically excerpting it', () => {
    const rationale = '売上増加。利益増加。成長市場。しかし返済資金を確認できないため見送る。<script>unsafe</script>'
    const html = render(view([{ ...newer, items: [{ ...newer.items[0], rationale }] }]))
    expect(html).toContain('しかし返済資金を確認できないため見送る。')
    expect(html).toContain('&lt;script&gt;unsafe&lt;/script&gt;')
    expect(html).not.toContain('<script>')
    expect(html).not.toContain('line-clamp')
  })

  it('uses the whole short headline only for an individual ticker and retains long reasoning at its exact link', () => {
    const rationale = '売上は伸びた。'.repeat(24) + 'ただし返済額が未確定のため買わない。'
    const item = { ...reviewedDeferFixture, rationale }
    const proposal = { ...older, headline: '返済後に残る資金が未確認のため、購入は保留。', items: [item] }
    const list = render(view([proposal]))
    expect(list).toContain(proposal.headline)
    expect(list).not.toContain('売上は伸びた')
    expect(render(view([proposal]), proposal.proposal_id, item.ticker)).toContain(rationale)
    const multi = { ...proposal, items: [item, buyFixture] }
    const other = render(view([multi]))
    expect(other).not.toContain(proposal.headline)
    expect(other).toContain(item.blockers[0].message)
    expect(other).toContain(buyFixture.rationale)
  })

  it('uses concise saved facts across actions instead of a misleading start of a long paragraph', () => {
    const long = '一見利益が増えたように見える。'.repeat(16)
    const summary = (item: SavedProposalView['items'][number]) => reportListSummary({
      proposal: { ...older, headline: long }, item: { ...item, rationale: long },
    })
    expect(summary(buyFixture)).toContain('指値で待つ案：1200.0000円・100株')
    expect(summary(partialBuyFixture)).toBe('買い指値案。作者の上限は1400.1000000000000001円。具体的な指値・数量は未確定。')
    const noAction = { ...reviewedDeferFixture, action: 'no_action' as const, assessment: null }
    expect(summary(noAction)).toContain('新規購入を見送った')
    expect(summary(holdFixture)).toContain('当初の投資仮説')
    expect(summary(sellFixture)).toContain('全量売却を提案')
    const manyUnknowns = { ...reviewedDeferFixture, blockers: [{ code: 'reason', message: long }] }
    expect(summary(manyUnknowns)).toBe('判断に必要な条件がそろわず保留。確認事項は個別レポートに記録。')
    const tiny = { ...reviewedDeferFixture, rationale: '買わない。' }
    expect(reportListSummary({ proposal: older, item: tiny })).toBe('買わない。')
  })

  it('labels corrected originals in list and detail without rewriting the old decision', () => {
    const corrected = { ...older, corrections: [{ correction_id: 'correction', erroneous_ref: { proposal_id: older.proposal_id, draft_sha256: '0'.repeat(64) }, replacement_ref: { proposal_id: 'replacement', draft_sha256: '1'.repeat(64) }, reason: '合成の訂正理由', recorded_at: '2026-11-15T00:00:00Z' }] }
    expect(render(view([corrected]))).toContain('訂正済みの旧提案')
    const html = render(view([corrected]), older.proposal_id, '1111')
    expect(html).toContain('表示中は訂正済みの旧提案です。採用候補ではありません。')
    expect(html).toContain(buyFixture.rationale)
  })

  it('keeps loading, empty and entirely unreadable histories distinct', () => {
    expect(render(null)).toContain('履歴を読み込んでいます')
    expect(render(view([]))).toContain('保存済みレポートはありません')
    const unavailable = render({ ...view([], ['broken']), status: 'unavailable' })
    expect(unavailable).toContain('読取不能の提案：1件')
    expect(unavailable).not.toContain('保存済みレポートはありません')
  })

  it('URL-encodes both immutable proposal identity and ticker', () => {
    expect(proposalReportHref('saved/a?b&c', '1234')).toBe('/proposal?proposal=saved%2Fa%3Fb%26c&ticker=1234')
  })
})

it('preserves exact report selection through repeated navigation and router Back/Forward', async () => {
  const data = view([newer, older])
  function HistoryRoute() {
    const [params] = useSearchParams()
    return createElement(ProposalHistory, { data, proposalId: params.get('proposal'), ticker: params.get('ticker') })
  }
  const router = createMemoryRouter([{ path: '/proposal', element: createElement(HistoryRoute) }], { initialEntries: ['/proposal'] })
  const html = () => renderToStaticMarkup(createElement(RouterProvider, { router }))
  expect(html()).toContain('3件のレポート')
  await router.navigate(proposalReportHref(older.proposal_id, '1111'))
  expect(html()).toContain(buyFixture.rationale)
  expect(html()).not.toContain(newer.items[0].rationale)
  await router.navigate(proposalReportHref(newer.proposal_id, '1111'))
  expect(html()).toContain(newer.items[0].rationale)
  expect(html()).not.toContain(buyFixture.rationale)
  await router.navigate(-1)
  expect(html()).toContain(buyFixture.rationale)
  await router.navigate(-1)
  expect(html()).toContain('3件のレポート')
  await router.navigate(1)
  expect(html()).toContain(buyFixture.rationale)
  router.dispose()
})
