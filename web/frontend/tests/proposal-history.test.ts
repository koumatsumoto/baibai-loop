import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createMemoryRouter, MemoryRouter, RouterProvider, useSearchParams } from 'react-router'
import { describe, expect, it, vi } from 'vitest'

import type { ProposalHistoryView, SavedProposalView } from '../src/api/types'
import { groupProposalReports, proposalActionLabel, proposalReportHref, publicationPeriod, reportListSummary } from '../src/lib/proposal-history'
import { ProposalHistory } from '../src/pages/ProposalHistoryPage'
import { buyFixture, holdFixture, holdingProposalFixture, newProposalFixture, partialBuyFixture, reviewedDeferFixture, sellFixture } from './fixtures/proposals'

function view(proposals: SavedProposalView[], missing: string[] = []): ProposalHistoryView {
  return { generated_at: '2026-11-20T00:00:00Z', status: missing.length ? 'partial' : (proposals.length ? 'ready' : 'empty'), error: missing.length ? '一部を読めません' : null, proposals, unavailable_proposal_ids: missing }
}
function render(data: unknown, proposalId: string | null = null, ticker: string | null = null) {
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
  it('formats each publication once and preserves different clocks within the same JST group', () => {
    const original = { ...older, published_at: '2026-11-14T02:41:09.999999Z' }
    const replacement = { ...newer, published_at: '2026-11-14T11:02:00+09:00' }
    const correction = {
      correction_id: 'group-clock-correction',
      erroneous_ref: { proposal_id: original.proposal_id, draft_sha256: '0'.repeat(64) },
      replacement_ref: { proposal_id: replacement.proposal_id, draft_sha256: '1'.repeat(64) },
      reason: '合成の訂正理由',
      recorded_at: '2026-11-15T00:00:00Z',
    }
    const format = vi.spyOn(Intl.DateTimeFormat.prototype, 'formatToParts')
    let html: string
    try {
      html = render(view([{ ...original, corrections: [correction] }, { ...replacement, corrections: [correction] }]))
      const times = new Set([Date.parse(original.published_at), Date.parse(replacement.published_at)])
      expect(format.mock.calls.filter(([instant]) => instant instanceof Date && times.has(instant.getTime()))).toHaveLength(2)
    } finally {
      format.mockRestore()
    }
    expect(html.match(/11\/14 午前/g)).toHaveLength(1)
    expect(html.match(/11:41 発行/g)).toHaveLength(2)
    expect(html.match(/11:02 発行/g)).toHaveLength(1)
    expect(html.indexOf('11:41 発行')).toBeLessThan(html.indexOf('11:02 発行'))
    expect(html.match(/訂正で候補から外れた提案/g)).toHaveLength(2)
  })

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

  it('shows exact saved buy price and its target session beside the matching decision only', () => {
    const terms = { ...buyFixture.terms!, target_session: '2026-11-19', limit_price_yen: '1190.0000000000000001' }
    const buy = { ...buyFixture, terms, rationale: '事業価値に対して価格条件を置いた。' }
    const sell = { ...sellFixture, terms: { ...terms, target_session: '2026-11-24', limit_price_yen: '1801.0000' } }
    const noAction = newProposalFixture.items.find(item => item.action === 'no_action')!
    const olderBuy = { ...older, as_of: '2026-11-18', published_at: '2026-11-18T20:00:00+09:00', items: [buy, partialBuyFixture, noAction] }
    const laterDefer = { ...newer, as_of: '2026-11-19', published_at: '2026-11-20T03:00:00+09:00' }
    const holding = { ...holdingProposalFixture, as_of: '2026-11-19', published_at: '2026-11-19T20:00:00+09:00', items: [sell] }
    const data = view([laterDefer, holding, olderBuy])
    const html = render(data)
    const target = '対象取引日 <time dateTime="2026-11-19">2026-11-19</time>'
    const card = (proposalId: string, ticker: string) => html
      .split(`href="/proposal?proposal=${proposalId}&amp;ticker=${ticker}"`)[1]?.split('</a>')[0]
    const buyCard = card(olderBuy.proposal_id, buy.ticker)!
    expect(buyCard).toContain(target)
    expect(buyCard).toContain('1190.0000000000000001 円')
    expect(buyCard.indexOf('買付（指値）')).toBeLessThan(buyCard.indexOf(target))
    expect(buyCard.indexOf(target)).toBeLessThan(buyCard.indexOf(buy.rationale))
    expect(buyCard).toContain('提案基準日 2026-11-18')
    expect(card(laterDefer.proposal_id, buy.ticker)).not.toContain('対象取引日')
    expect(card(olderBuy.proposal_id, partialBuyFixture.ticker)).not.toContain('対象取引日')
    expect(card(olderBuy.proposal_id, partialBuyFixture.ticker)).toContain('買付案（保存時の具体条件なし）')
    expect(card(olderBuy.proposal_id, noAction.ticker)).not.toContain('対象取引日')
    expect(card(holding.proposal_id, sell.ticker)).not.toContain('対象取引日')
    expect(html).not.toContain('1801.0000')
    expect(html).not.toMatch(/期限切れ|失効済み|注文可能/)
    const detail = render(data, olderBuy.proposal_id, buy.ticker)
    const [top, rest] = detail.split('data-proposal-details')
    expect(top).toContain(target)
    expect(top).toContain('1190.0000000000000001 円')
    expect(rest).toContain('保存時の対象取引日')
    expect(rest).toContain('1190.0000000000000001 円')
    expect(rest).toContain('dateTime="2026-11-19"')
    expect(detail).not.toMatch(/期限切れ|失効済み|注文可能/)
  })

  it('keeps the later economic buy distinct from the earlier order terms of the same company', () => {
    const conditional = {
      ...partialBuyFixture, ticker: buyFixture.ticker,
      economic_decision: 'price_conditional_allocation' as const,
      terms_unavailable_reasons: [{ code: 'order_session_unknown', message: '保存時には翌立会を確認できなかった。' }],
    }
    const legacy = JSON.parse(JSON.stringify(partialBuyFixture))
    delete legacy.economic_decision
    expect(proposalActionLabel(legacy)).toBe('買付案（保存時の具体条件なし）')
    const a = { ...older, items: [buyFixture] }
    const b = { ...newer, items: [conditional] }
    const data = view([b, a])
    const list = render(data)
    const card = (id: string) => list.split(`href="/proposal?proposal=${id}&amp;ticker=${buyFixture.ticker}"`)[1]?.split('</a>')[0]
    expect(card(b.proposal_id)).toContain('価格条件付きの買付方針（注文条件未確認）')
    expect(card(b.proposal_id)).not.toContain('対象取引日')
    expect(card(a.proposal_id)).toContain('保存時の買付（指値）')
    expect(card(a.proposal_id)).toContain(buyFixture.terms!.target_session)
    const detail = render(data, b.proposal_id, buyFixture.ticker)
    expect(detail).toContain(conditional.terms_unavailable_reasons[0].message)
    expect(detail).toContain(conditional.proposed_limit_yen)
    expect(detail).not.toContain('保存時の指値区分')
    expect(detail).not.toContain('保存時の対象取引日')
    expect(detail).not.toContain('保存指値・選択数量の金額比較')
    expect(render(data, a.proposal_id, buyFixture.ticker)).toContain('保存指値・選択数量の金額比較')
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
    expect(summary(buyFixture)).toBe('保存時の指値で待つ案：100株。')
    expect(summary(partialBuyFixture)).toBe('作者が示した価格上限は1400.1000000000000001円。注文の指値・数量は未確定。')
    const noAction = { ...reviewedDeferFixture, action: 'no_action' as const, assessment: null }
    expect(summary(noAction)).toContain('保存時の判断は新規購入の見送り')
    expect(summary(holdFixture)).toContain('今回の保有仮説')
    expect(summary(sellFixture)).toContain('全量売却を提案')
    const manyUnknowns = { ...reviewedDeferFixture, blockers: [{ code: 'reason', message: long }] }
    expect(summary(manyUnknowns)).toBe('判断に必要な条件がそろわず保留。確認事項は個別レポートに記録。')
    const tiny = { ...reviewedDeferFixture, rationale: '買わない。' }
    expect(reportListSummary({ proposal: older, item: tiny })).toBe('買わない。')
  })

  it.each(['legacy', 'reviewed rebuttal'] as const)('does not invent comparisons or human non-purchase for %s', origin => {
    const base = newProposalFixture.items.find(item => item.action === 'no_action')!
    const rationale = '体制変更の開示に記載された当時の原文と前提を整理した。'.repeat(5)
      + '中核契約の終了を確認し、新規の資金配分は提案しない。'
    const original = base.assessment!
    const item = {
      ...base, rationale,
      assessment: origin === 'legacy' ? null : {
        ...original, stance: 'unsupported' as const, investment_case_status: 'broken' as const,
        investment_case_status_reason: '保存一次資料で中核契約の終了を確認。',
        valuation: { status: 'unresolved' as const, reason: '比較用の金額は計算しない。' },
        review: { ...original.review, primary_source_check: 'verified' as const },
      },
    }
    const proposal = { ...older, headline: rationale, items: [item] }
    const data = view([proposal])
    const list = render(data)
    expect(list).toContain('保存時の判断は新規購入の見送り')
    expect(list).not.toMatch(/他の選択肢と比較|購入を見送った|購入しなかった|人間の報告/)
    const detail = render(data, proposal.proposal_id, item.ticker)
    expect(detail).toContain(rationale)
    expect(detail).not.toContain('人間の報告')
    if (origin === 'reviewed rebuttal') expect(detail).toContain('保存一次資料で中核契約の終了を確認。')
  })

  it('keeps a supported quote separate from the saved choice to retain the same capital', () => {
    const base = newProposalFixture.items.find(item => item.action === 'no_action')!
    const original = base.assessment!
    const rationale = '取得権と分配時期を確認し、同じ元手・期間の資金経路を照合した。'.repeat(5)
      + 'ただし期日前の確定支払いを優先し、現金を残す。'
    const scenario = { terminal_value_per_share_yen: '0', cash_distribution_per_share_yen: '1300',
      annualized_return_pct: '18.181818', calculation: '合成の受領額。', source_ids: null, sources: null }
    const item = {
      ...base, rationale,
      assessment: {
        ...original, stance: 'uncertain' as const, investment_case_status: 'uncertain' as const,
        valuation: {
          status: 'resolved' as const, price_yen: '1100', price_as_of: '2026-10-26',
          price_observed_at: null, price_basis: 'last_close_unadjusted' as const,
          horizon_months: 12, required_annual_return_pct: '6', base: scenario, downside: scenario,
        },
        price_value_case: {
          status: 'supported_at_quote' as const,
          observed_quote: { fact_id: 'quote', price_yen: '1100', as_of: '2026-10-26',
            observed_at: null, price_basis: 'last_close_unadjusted' as const, source_ids: [] },
          price_compatible_conditions: '同じ期限内の分配を取得できる場合。',
          price_only_condition: null, acquisition_limit: null, value_facts: [], sources: [],
        },
      },
    }
    const proposal = { ...older, headline: rationale, items: [item] }
    const data = view([proposal])
    const list = render(data)
    expect(list).toContain('保存時の判断は新規購入の見送り')
    expect(list).not.toMatch(/中心ケース|要求年率|他の選択肢と比較|購入を見送った/)
    const short = '期限前の確定支払いを優先し、現金を残す。'
    expect(reportListSummary({ proposal, item: { ...item, rationale: short } })).toBe(short)
    const detail = render(data, proposal.proposal_id, item.ticker)
    expect(detail).toContain(rationale)
    expect(detail).toContain('この価格で割安を支持')
    expect(detail).toContain('保存評価の見返り')
    expect(detail).toContain('経済的見送り')
  })

  it('labels corrected originals in list and detail without rewriting the old decision', () => {
    const corrected = { ...older, corrections: [{ correction_id: 'correction', erroneous_ref: { proposal_id: older.proposal_id, draft_sha256: '0'.repeat(64) }, replacement_ref: { proposal_id: 'replacement', draft_sha256: '1'.repeat(64) }, reason: '合成の訂正理由', recorded_at: '2026-11-15T00:00:00Z' }] }
    const list = render(view([corrected]))
    expect(list).toContain('訂正で候補から外れた提案')
    expect(list.indexOf('訂正で候補から外れた提案')).toBeLessThan(list.indexOf('対象取引日'))
    expect(list).toContain('1200.0000 円')
    const html = render(view([corrected]), older.proposal_id, '1111')
    expect(html).toContain('表示中は訂正で候補から外れた提案です。採用候補ではありません。')
    expect(html.indexOf('採用候補ではありません。')).toBeLessThan(html.indexOf('対象取引日'))
    expect(html).toContain(buyFixture.rationale)
  })

  it.each([
    ['corrections', undefined], ['corrections', null], ['corrections', {}],
    ['human_reports', undefined], ['human_reports', null], ['human_reports', {}],
  ] as const)('withholds the exact report when %s is not an array (%j)', (field, value) => {
    // Serializing undefined removes the property just as in a sparse response.
    const malformed = JSON.parse(JSON.stringify({ ...older, [field]: value })) as SavedProposalView
    const data = view([newer, malformed], ['unreadable-at-source'])
    const list = render(data)
    expect(list).toContain('配信データの形式が不正な提案が1件あります。')
    expect(list).toContain('読取不能の提案が1件あります。')
    expect(list).toContain('href="/proposal?proposal=proposal-new&amp;ticker=1111"')
    expect(list).not.toContain('href="/proposal?proposal=proposal-old')
    expect(render(data, newer.proposal_id, '1111')).toContain(newer.items[0].rationale)

    const detail = render(data, older.proposal_id, '1111')
    expect(detail).toContain('訂正・人間の報告を確認できないため、この保存レポートは表示していません。')
    expect(detail).not.toContain(buyFixture.rationale)
    const allMalformed = render(view([malformed]))
    expect(allMalformed).toContain('配信データの形式が不正な提案が1件あります。')
    expect(allMalformed).toContain('表示できるレポート：0件')
    expect(allMalformed).not.toContain('保存済みレポートはありません')
  })

  it('keeps loading, empty and entirely unreadable histories distinct', () => {
    const loading = render(undefined)
    const empty = render(view([]))
    const unavailable = render({ ...view([], ['broken']), status: 'unavailable' })
    expect(loading).toContain('履歴を読み込んでいます')
    expect(empty).toContain('保存済みレポートはありません')
    expect(empty).toContain('表示データ生成')
    expect(unavailable).toContain('売買提案の読取不能')
    expect(unavailable).toContain('読取不能の提案：1件')
    expect(unavailable).not.toContain('保存済みレポートはありません')
    for (const html of [loading, empty, unavailable]) expect(html).not.toContain('0 円')
  })

  it.each([
    ['JSON null', null],
    ['ready without rows', { ...view([]), status: 'ready' }],
    ['partial without rows', view([], ['unreadable'])],
    ['empty with rows', { ...view([newer]), status: 'empty' }],
    ['missing unavailable IDs', { ...view([newer]), unavailable_proposal_ids: undefined }],
  ] as const)('shows a read failure for a contradictory history envelope: %s', (_, wire) => {
    const data = JSON.parse(JSON.stringify(wire)) as ProposalHistoryView
    for (const html of [render(data), render(data, newer.proposal_id, '1111')]) {
      expect(html).toContain('売買提案の読取不能')
      expect(html).not.toContain('保存済みレポートはありません')
      expect(html).not.toContain(newer.items[0].rationale)
    }
  })

  it.each([
    ['null proposal', null],
    ['missing items', { ...older, items: undefined }],
    ['null item', { ...older, items: [null] }],
    ['invalid publication instant', { ...older, published_at: 'not-a-date' }],
  ] as const)('withholds a malformed row without hiding a readable sibling: %s', (_, row) => {
    const data = JSON.parse(JSON.stringify({ ...view([newer]), proposals: [row, newer] }))
    const list = render(data)
    expect(list).toContain('配信データの形式が不正な提案が1件あります')
    expect(list).toContain(proposalReportHref(newer.proposal_id, '1111').replace('&', '&amp;'))
    expect(list).not.toContain('proposal=proposal-old')
    expect(render(data, newer.proposal_id, '1111')).toContain(newer.items[0].rationale)

    const pinned = render(data, older.proposal_id, '1111')
    expect(pinned).toContain('指定された保存レポートを確認できません')
    expect(pinned).not.toContain(newer.items[0].rationale)
    const noneReadable = render({ ...data, proposals: [row] })
    expect(noneReadable).not.toContain('保存済みレポートはありません')
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


it('keeps allocation comparisons distinct from an entire sale', () => {
  expect(proposalActionLabel(sellFixture)).toBe('全量売却')
  const reasons = ['consider_partial_recovery', 'consider_full_recovery'] as const
  for (const reason_code of reasons) {
    const item = { ...sellFixture, rationale: '長'.repeat(101), reason_code }
    const result = reportListSummary({ proposal: { ...older, items: [item, holdFixture] }, item })
    expect(result).toContain('注文条件は提示していない')
    expect(result).not.toContain('全量売却を提案')
  }
  expect(proposalActionLabel({ ...sellFixture, reason_code: 'consider_partial_recovery' })).toBe('一部回収の条件案')
  expect(proposalActionLabel({ ...sellFixture, reason_code: 'consider_full_recovery' })).toBe('全量回収の条件案')
})
