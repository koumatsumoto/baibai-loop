import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, expect, it, vi } from 'vitest'

import { ApiError, fetchJson } from '../src/api/client'
import type { LatestUSProposalView } from '../src/api/types'
import { USProposal, USProposalPage } from '../src/pages/USProposalPage'

const fixture: LatestUSProposalView = {
  generated_at: '2026-11-28T18:00:00Z',
  status: 'ready',
  error: null,
  proposal: {
    proposal_id: 'us-proposal-fixture',
    draft_sha256: 'a'.repeat(64),
    ticker: 'GOOGL',
    venue: 'XNAS',
    share_class: 'Class A common',
    currency: 'USD',
    session_et: '2026-11-27',
    cutoff_at: '2026-11-28T16:00:00Z',
    published_at: '2026-11-28T17:10:00Z',
    reviewed_at: '2026-11-28T17:00:00Z',
    action: 'no_action',
    reason: '現金に比べ下振れの根拠が薄い。',
    revisit_when: '次回決算の確認後に見直す。',
    official_close: { price_usd: '120.01', origin: 'listing_exchange', observed_at: '2026-11-27T18:05:00Z' },
    iex_band: { first_session_et: '2026-11-02', last_session_et: '2026-11-27', observations: 19, low_usd: '110.900', high_usd: '125.03', last_usd: '120.03' },
    comparison_horizon_et: '2027-02-15',
    unresolved: [],
  },
}

const hooks = vi.hoisted(() => ({
  setup: null as null | (() => () => void),
  setState: vi.fn(),
}))
vi.mock('react', async original => ({
  ...await original<typeof import('react')>(),
  useState: (initial: unknown) => [initial, hooks.setState],
  useEffect: (setup: () => () => void) => { hooks.setup = setup },
}))
vi.mock('../src/api/client', async original => ({
  ...await original<typeof import('../src/api/client')>(),
  fetchJson: vi.fn(),
}))

afterEach(() => {
  vi.resetAllMocks()
  hooks.setup = null
})

it('renders the same saved ID and SHA, keeps USD price evidence distinct, and has no order controls', () => {
  const html = renderToStaticMarkup(createElement(USProposal, { state: { kind: 'loaded', data: fixture } }))
  expect(html).toContain('GOOGL · XNAS · USD')
  expect(html).toContain('見送り')
  expect(html).toContain('現金に比べ下振れの根拠が薄い。')
  expect(html).toContain('次回決算の確認後に見直す。')
  expect(html).toContain('取引所の無調整終値')
  expect(html).not.toContain('Alpaca SIP経由')
  expect(html).toContain('USD 120.01')
  expect(html).toContain('Alpaca IEXの分割調整済み観測')
  expect(html).toContain('19件の観測、USD 110.900〜125.03')
  expect(html).toContain('2027-02-15')
  expect(html).toContain('2026-11-29 02:10')
  expect(html).toContain('us-proposal-fixture')
  expect(html).toContain('a'.repeat(64))
  expect(html).not.toMatch(/<button|<form|href|JPY/)
})

it('labels a relayed Nasdaq official close as Alpaca SIP, distinct from direct and IEX evidence', () => {
  if (fixture.proposal === null) throw new Error('test fixture requires a proposal')
  const data: LatestUSProposalView = {
    ...fixture,
    proposal: {
      ...fixture.proposal,
      official_close: { origin: 'alpaca_sip_nasdaq_official_close', price_usd: '120.0100', observed_at: '2026-11-27T18:05:00Z' },
    },
  }
  const html = renderToStaticMarkup(createElement(USProposal, { state: { kind: 'loaded', data } }))
  expect(html).toMatch(/Nasdaqの無調整公式終値（Alpaca SIP経由）<\/dt><dd[^>]*>USD 120\.0100<\/dd>/)
  expect(html).toContain('Alpaca IEXの分割調整済み観測')
  expect(html).toContain('見送り')
  expect(html).not.toContain('取引所の無調整終値')
})

it('does not copy the IEX price into the official-close slot for a deferred decision', () => {
  if (fixture.proposal === null) throw new Error('test fixture requires a proposal')
  const data: LatestUSProposalView = {
    ...fixture,
    proposal: { ...fixture.proposal, action: 'defer', official_close: null, comparison_horizon_et: null, unresolved: ['取引所終値を確認できていない。'] },
  }
  const html = renderToStaticMarkup(createElement(USProposal, { state: { kind: 'loaded', data } }))
  expect(html).toContain('判断保留')
  expect(html).toMatch(/取引所の無調整終値<\/dt><dd[^>]*>未確認<\/dd>/)
  expect(html).toContain('USD 110.900〜125.03')
  expect(html).toContain('取引所終値を確認できていない。')
  expect(html).not.toContain('見送り')
  expect(html).not.toContain('USD 120.01')
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
  ['missing nested close', { ...fixture.proposal, official_close: undefined }],
  ['unknown official origin', { ...fixture.proposal, official_close: { ...fixture.proposal?.official_close, origin: 'other_feed' } }],
  ['missing official origin', { ...fixture.proposal, official_close: { ...fixture.proposal?.official_close, origin: undefined } }],
  ['SIP Nasdaq origin with a non-Nasdaq listing', { ...fixture.proposal, venue: 'XNYS', official_close: { ...fixture.proposal?.official_close, origin: 'alpaca_sip_nasdaq_official_close' } }],
  ['incomplete no-action basis', { ...fixture.proposal, official_close: null, comparison_horizon_et: null }],
  ['unusable saved clock', { ...fixture.proposal, published_at: 'unknown' }],
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
  const cleanup = hooks.setup?.()
  expect(fetchJson).toHaveBeenCalledExactlyOnceWith('/api/us-proposal/latest', { signal: expect.any(AbortSignal) })
  await Promise.resolve()
  expect(hooks.setState).toHaveBeenCalledExactlyOnceWith({ kind: 'loaded', data: fixture })
  cleanup?.()
  expect(vi.mocked(fetchJson).mock.calls[0]?.[1]?.signal?.aborted).toBe(true)
})

it.each([[404, 'pending'], [500, 'unavailable']])('classifies an HTTP %s without falling back to Japan', async (status, kind) => {
  vi.mocked(fetchJson).mockRejectedValue(new ApiError(status, 'internal detail'))
  USProposalPage()
  const cleanup = hooks.setup?.()
  await Promise.resolve()
  await Promise.resolve()
  expect(hooks.setState).toHaveBeenCalledExactlyOnceWith({ kind })
  expect(fetchJson).toHaveBeenCalledExactlyOnceWith('/api/us-proposal/latest', { signal: expect.any(AbortSignal) })
  cleanup?.()
})
