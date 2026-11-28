import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, expect, it, vi } from 'vitest'

import { ApiError, fetchJson } from '../src/api/client'
import type { LatestUSProposalView } from '../src/api/types'
import { USProposal, USProposalPage } from '../src/pages/USProposalPage'
import { usProposalFixture as fixture } from './fixtures/us-proposal'

const hooks = vi.hoisted(() => ({
  effects: [] as (() => void | (() => void))[],
  params: new URLSearchParams(),
  setState: vi.fn(),
}))
vi.mock('react', async original => ({
  ...await original<typeof import('react')>(),
  useState: (initial: unknown) => [initial, hooks.setState],
  useEffect: (setup: () => void | (() => void)) => { hooks.effects.push(setup) },
}))
vi.mock('react-router', async original => ({
  ...await original<typeof import('react-router')>(),
  useSearchParams: () => [hooks.params],
}))
vi.mock('../src/api/client', async original => ({
  ...await original<typeof import('../src/api/client')>(),
  fetchJson: vi.fn(),
}))

afterEach(() => {
  vi.resetAllMocks()
  vi.unstubAllGlobals()
  hooks.effects = []
  hooks.params = new URLSearchParams()
})

it('renders the same saved ID and SHA, keeps USD price evidence distinct, and has no order controls', () => {
  const html = renderToStaticMarkup(createElement(USProposal, { state: { kind: 'loaded', data: fixture } }))
  expect(html).toContain('GOOGL · XNAS · USD')
  expect(html).toContain('見送り')
  expect(html).toContain('現金に比べ下振れの根拠が薄い。')
  expect(html).toContain('次回決算の確認後に見直す。')
  expect(html).toContain('取引所の無調整終値')
  expect(html).not.toContain('Alpaca SIP経由')
  expect(html).toContain('USD 100.50')
  expect(html).toContain('Alpaca IEXの分割調整済み観測')
  expect(html).toContain('2026-11-27（米東部時間）に19件の観測、USD 90.900〜125.03')
  expect(html).toContain('2027-02-15')
  expect(html).toContain('情報締切（JST）')
  expect(html).toContain('2026-11-29 01:00')
  expect(html).toContain('終値の取得時刻（JST）')
  expect(html).toContain('2026-11-28 03:05')
  expect(html).toContain('正式判断の査読日時（JST）')
  expect(html).toContain('2026-11-29 02:00')
  expect(html).toContain('発行日時（JST）')
  expect(html).toContain('2026-11-29 02:10')
  expect(html).toContain('期間末尾：USD 101.03')
  expect(html).toContain('同期間のUSD現金との比較')
  expect(html).toContain('無利息のUSD現金')
  expect(html).toContain('税・費用・為替')
  expect(html).toContain('USD 105.75')
  expect(html).toContain('USD 1.25')
  expect(html).toContain('USD +6.50')
  expect(html).toContain('USD -20.50')
  expect(html).toContain('us-proposal-fixture')
  expect(html).toContain('a'.repeat(64))
  expect(html).not.toMatch(/<button|<form|href|JPY/)
})

it.each(['no_action', 'defer'] as const)('shows the SIP receipt and its own USD comparison for %s', action => {
  if (fixture.proposal?.comparison == null) throw new Error('test fixture requires saved comparison')
  const received = '2026-11-28T10:05:10.125Z'
  const data: LatestUSProposalView = {
    ...fixture,
    proposal: {
      ...fixture.proposal,
      action,
      comparison_price: { origin: 'alpaca_sip_nasdaq_official_close', price_usd: '100.5000', observed_at: received },
      comparison: {
        ...fixture.proposal.comparison,
        base: {
          terminal_value_usd_per_share: '100.50',
          eligible_distributions_usd_per_share: '1.0E-13',
          difference_from_cash_usd_per_share: '1.0E-13',
        },
        downside: {
          terminal_value_usd_per_share: '80',
          eligible_distributions_usd_per_share: '0E-14',
          difference_from_cash_usd_per_share: '-20.50000000000000',
        },
      },
    },
  }
  const html = renderToStaticMarkup(createElement(USProposal, { state: { kind: 'loaded', data } }))
  expect(html).toMatch(/Nasdaqの無調整公式終値（Alpaca SIP経由）<\/dt><dd[^>]*>USD 100\.5000 \/ 株<\/dd>/)
  expect(html).toContain('終値データの受領時刻（JST）')
  expect(html).toContain(`dateTime="${received}" title="${received}">2026-11-28 19:05`)
  expect(html).toContain('Alpaca IEXの分割調整済み観測')
  expect(html).toContain('期間末尾：USD 101.03')
  expect(html).toContain(action === 'no_action' ? '見送り' : '判断保留')
  expect(html).toContain('上記の基準価格1株分のUSD 100.5000')
  expect(html).toContain('USD +1.0E-13')
  expect(html).toContain('USD 0E-14')
  expect(html).not.toContain('取引所の無調整終値')
  expect(html).not.toContain('終値の取得時刻（JST）')
  expect(html).not.toMatch(/<button|<form|href|JPY/)
})

it.each(['XNAS', 'XNYS'] as const)('uses the raw SIP daily close for %s without claiming an exchange official close', venue => {
  if (fixture.proposal === null) throw new Error('test fixture requires a proposal')
  const data: LatestUSProposalView = {
    ...fixture,
    proposal: {
      ...fixture.proposal,
      venue,
      comparison_price: { origin: 'alpaca_sip_daily_close', price_usd: '100.50', observed_at: '2026-11-27T18:33:00Z' },
      iex_band: null,
    },
  }
  const html = renderToStaticMarkup(createElement(USProposal, { state: { kind: 'loaded', data } }))
  expect(html).toContain('見送り')
  expect(html).toContain('Alpaca SIPの無調整日足終値')
  expect(html).toContain('対象立会日（米東部時間）')
  expect(html).toContain('終値データの受領時刻（JST）')
  expect(html).toContain('取引所の公式終値・約定値とは別です。')
  expect(html).toContain('上記の基準価格1株分のUSD 100.50')
  expect(html).not.toContain('取引所の無調整終値')
  expect(html).not.toContain('Nasdaqの無調整公式終値')
  expect(html).not.toContain('Alpaca IEXの分割調整済み観測')
  expect(html).not.toContain('比較未確定')
})

it('does not copy the IEX price into the official-close slot for a deferred decision', () => {
  if (fixture.proposal === null) throw new Error('test fixture requires a proposal')
  const data: LatestUSProposalView = {
    ...fixture,
    proposal: { ...fixture.proposal, action: 'defer', comparison_price: null, comparison_horizon_et: null, comparison: null, unresolved: ['取引所終値を確認できていない。'] },
  }
  const html = renderToStaticMarkup(createElement(USProposal, { state: { kind: 'loaded', data } }))
  expect(html).toContain('判断保留')
  expect(html).toMatch(/比較の基準価格<\/dt><dd[^>]*>未確認<\/dd>/)
  expect(html).toContain('USD 90.900〜125.03')
  expect(html).toContain('取引所終値を確認できていない。')
  expect(html).not.toContain('見送り')
  expect(html).not.toContain('USD 100.50')
  expect(html).toContain('比較未確定')
  expect(html).not.toContain('現金との差')
})

it('keeps the compared numbers and horizon on a deferred report with another limitation', () => {
  if (fixture.proposal?.comparison == null) throw new Error('test fixture requires saved comparison')
  const data: LatestUSProposalView = {
    ...fixture,
    proposal: {
      ...fixture.proposal,
      action: 'defer',
      reason: '事業前提が未確認のため保留。',
      comparison: {
        ...fixture.proposal.comparison,
        base: { ...fixture.proposal.comparison.base, terminal_value_usd_per_share: '105.7500000000001', difference_from_cash_usd_per_share: '6.5000000000001' },
      },
    },
  }
  const html = renderToStaticMarkup(createElement(USProposal, { state: { kind: 'loaded', data } }))
  expect(html).toContain('判断保留')
  expect(html).toContain('事業前提が未確認のため保留。')
  expect(html).toContain('2027-02-15')
  expect(html).toContain('USD 105.7500000000001')
  expect(html).toContain('USD +6.5000000000001')
  expect(html).not.toContain('比較未確定')
})

it('displays the serialized exponent form of small exact values without treating them as zero', () => {
  if (fixture.proposal?.comparison == null) throw new Error('test fixture requires saved comparison')
  const data: LatestUSProposalView = {
    ...fixture,
    proposal: {
      ...fixture.proposal,
      comparison: {
        ...fixture.proposal.comparison,
        base: {
          terminal_value_usd_per_share: '100.50',
          eligible_distributions_usd_per_share: '1.0E-13',
          difference_from_cash_usd_per_share: '1.0E-13',
        },
        downside: { ...fixture.proposal.comparison.downside, eligible_distributions_usd_per_share: '0E-14' },
      },
    },
  }
  const html = renderToStaticMarkup(createElement(USProposal, { state: { kind: 'loaded', data } }))
  expect(html).toContain('USD 1.0E-13')
  expect(html).toContain('USD +1.0E-13')
  expect(html).toContain('USD 0E-14')
  expect(html).not.toContain('比較未確定')
})

it('separates an empty App from pending publication and unavailable reads, without showing source errors', () => {
  const empty = renderToStaticMarkup(createElement(USProposal, {
    state: { kind: 'loaded', data: { ...fixture, status: 'empty', proposal: null } },
  }))
  const pending = renderToStaticMarkup(createElement(USProposal, { state: { kind: 'pending' } }))
  const unavailable = renderToStaticMarkup(createElement(USProposal, {
    state: { kind: 'loaded', data: { ...fixture, status: 'unavailable', error: 'internal storage detail', proposal: null } },
  }))
  const incomplete = renderToStaticMarkup(createElement(USProposal, {
    state: { kind: 'loaded', data: { ...fixture, proposal: null } },
  }))
  expect(empty).toContain('保存済みの米国株の判断はありません')
  expect(pending).toContain('米国株の判断は配信準備中です')
  expect(unavailable).toContain('米国株の判断を読み取れません')
  expect(incomplete).toContain('米国株の判断を読み取れません')
  expect(`${pending}${unavailable}${incomplete}`).not.toContain('保存済みの米国株の判断はありません')
  expect(unavailable).not.toContain('internal storage detail')
})

it.each([
  ['partial root', {}],
  ['other market currency', { ...fixture.proposal, currency: 'JPY' }],
  ['unknown action', { ...fixture.proposal, action: 'buy' }],
  ['missing nested close', { ...fixture.proposal, comparison_price: undefined }],
  ['unknown official origin', { ...fixture.proposal, comparison_price: { ...fixture.proposal?.comparison_price, origin: 'other_feed' } }],
  ['missing official origin', { ...fixture.proposal, comparison_price: { ...fixture.proposal?.comparison_price, origin: undefined } }],
  ['SIP Nasdaq origin with a non-Nasdaq listing', { ...fixture.proposal, venue: 'XNYS', comparison_price: { ...fixture.proposal?.comparison_price, origin: 'alpaca_sip_nasdaq_official_close' } }],
  ['incomplete no-action basis', { ...fixture.proposal, comparison_price: null, comparison_horizon_et: null }],
  ['unusable saved clock', { ...fixture.proposal, published_at: 'unknown' }],
  ['unusable information cutoff', { ...fixture.proposal, cutoff_at: 'unknown' }],
  ['unusable formal review clock', { ...fixture.proposal, reviewed_at: 'unknown' }],
  ['unusable official acquisition time', { ...fixture.proposal, comparison_price: { ...fixture.proposal?.comparison_price, observed_at: 'unknown' } }],
  ['unusable IEX last', { ...fixture.proposal, iex_band: { ...fixture.proposal?.iex_band, last_usd: 101.03 } }],
  ['stale absent comparison', { ...fixture.proposal, action: 'defer', comparison: undefined }],
  ['missing horizon with a comparison', { ...fixture.proposal, action: 'defer', comparison_horizon_et: null }],
  ['comparison without its own official close', { ...fixture.proposal, action: 'defer', comparison_price: null }],
  ['unsaved comparison basis', { ...fixture.proposal, comparison: { ...fixture.proposal?.comparison, cash_basis: 'interest_bearing' } }],
  ['broken scenario value', { ...fixture.proposal, comparison: { ...fixture.proposal?.comparison, base: { ...fixture.proposal?.comparison?.base, difference_from_cash_usd_per_share: 'unknown' } } }],
])('treats a ready response with %s as unreadable without inventing a decision', (_label, proposal) => {
  // The authenticated response is still external to the UI; a missing serving field is not a valid value.
  const data = { ...fixture, proposal } as LatestUSProposalView
  const html = renderToStaticMarkup(createElement(USProposal, { state: { kind: 'loaded', data } }))
  expect(html).toContain('米国株の判断を読み取れません')
  expect(html).not.toContain('保存済みの米国株の判断はありません')
  expect(html).not.toContain('GOOGL')
  expect(html).not.toContain('判断保留')
})

it('fetches the one fixed key only with the existing API client and aborts on exit', async () => {
  vi.mocked(fetchJson).mockResolvedValue(fixture)
  expect(renderToStaticMarkup(USProposalPage())).toContain('米国株の判断を読み込んでいます')
  const cleanup = hooks.effects[0]?.()
  expect(fetchJson).toHaveBeenCalledExactlyOnceWith('/api/us-proposal/latest', { signal: expect.any(AbortSignal) })
  await Promise.resolve()
  expect(hooks.setState).toHaveBeenCalledExactlyOnceWith({ kind: 'loaded', data: fixture })
  cleanup?.()
  expect(vi.mocked(fetchJson).mock.calls[0]?.[1]?.signal?.aborted).toBe(true)
})

it.each([[404, 'pending'], [500, 'unavailable']])('classifies an HTTP %s without falling back to Japan', async (status, kind) => {
  vi.mocked(fetchJson).mockRejectedValue(new ApiError(status, 'internal detail'))
  USProposalPage()
  const cleanup = hooks.effects[0]?.()
  await Promise.resolve()
  await Promise.resolve()
  expect(hooks.setState).toHaveBeenCalledExactlyOnceWith({ kind })
  expect(fetchJson).toHaveBeenCalledExactlyOnceWith('/api/us-proposal/latest', { signal: expect.any(AbortSignal) })
  cleanup?.()
})

it('starts the requested exact report at its summary', () => {
  const scrollTo = vi.fn()
  vi.stubGlobal('window', { scrollTo })
  hooks.params = new URLSearchParams({ proposal: 'us-proposal-fixture', ticker: 'GOOGL' })
  USProposalPage()
  hooks.effects[1]?.()
  expect(scrollTo).toHaveBeenCalledExactlyOnceWith(0, 0)
})
