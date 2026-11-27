import type { LatestUSProposalView } from '../../src/api/types'

export const usProposalFixture: LatestUSProposalView = {
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
    official_close: { price_usd: '100.50', origin: 'listing_exchange', observed_at: '2026-11-27T18:05:00Z' },
    iex_band: { first_session_et: '2026-11-02', last_session_et: '2026-11-27', observations: 19, low_usd: '90.900', high_usd: '125.03', last_usd: '101.03' },
    comparison_horizon_et: '2027-02-15',
    comparison: {
      cash_basis: 'same_usd_no_interest_tax_or_fees',
      base: {
        terminal_value_usd_per_share: '105.75', eligible_distributions_usd_per_share: '1.25', difference_from_cash_usd_per_share: '6.50',
      },
      downside: {
        terminal_value_usd_per_share: '80', eligible_distributions_usd_per_share: '0', difference_from_cash_usd_per_share: '-20.50',
      },
    },
    unresolved: [],
  },
  history: { status: 'ready', error: null, proposals: [], unavailable_proposal_ids: [], has_more: false },
}
