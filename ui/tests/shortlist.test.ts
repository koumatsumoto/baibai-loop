import { describe, expect, it } from 'vitest'

import type {
  CandidateRowView,
  MachineSelectionView,
  SelectionLonglistEntryView,
  ShortlistEntryView,
  ShortlistNarrativeView,
  ShortlistView,
} from '../src/api/types'
import { buildShortlistComparison, rankDivergence, summarize } from '../src/lib/shortlist'

function narrative(overrides: Partial<ShortlistNarrativeView> = {}): ShortlistNarrativeView {
  return {
    ploss: '中低',
    why: '受注端境で売られている',
    temporary: '翌期に戻る',
    structural: '毀損はない',
    survive: 'net cash で耐える',
    unlock: '還元強化',
    counter: '構造鈍化の可能性',
    research: '受注残を確認',
    value: 'FV 乖離が大きい',
    prov: '深掘り最優先',
    upside: '正常化で PER12 倍相当',
    downside: '簿価が床',
    rr: '下値が資産で支えられ上値は倍近い',
    catalyst: '2Q 決算で受注残の回復',
    catalyst_date: '2026-08-06',
    macro: 'sizing caution は該当なし',
    sector_label: null,
    ...overrides,
  }
}

function entry(overrides: Partial<ShortlistEntryView> = {}): ShortlistEntryView {
  return {
    ticker: '2331',
    decision: 'selected',
    reason: '一次 IR へ進める',
    rank: 1,
    narrative: narrative(),
    ...overrides,
  }
}

function shortlist(entries: ShortlistEntryView[]): ShortlistView {
  return {
    shortlist_id: 'shortlist-20260721-test',
    selection_id: 'selection-test',
    run_revision_id: 'runrev-test',
    as_of: '2026-07-21',
    published_at: '2026-07-21T15:00:00+09:00',
    entries,
    unreadable_entries: 0,
  }
}

function longlistEntry(
  overrides: Partial<SelectionLonglistEntryView> = {},
): SelectionLonglistEntryView {
  return {
    rank: 3,
    ticker: '2331',
    name: 'ALSOK',
    market_price_yen: 1000,
    fair_value_anchor_yen: 1250,
    fair_value_gap_pct: 25,
    expected_return_pct: 10.8,
    screening_playbook: null,
    liquidity_status: 'ok',
    selection_reasons: [],
    durability_warnings: [],
    event_warnings: [],
    ...overrides,
  }
}

function selection(longlist: SelectionLonglistEntryView[]): MachineSelectionView {
  return {
    selection_id: 'selection-test',
    run_revision_id: 'runrev-test',
    profile: 'value',
    macro_context_id: null,
    created_at: '2026-07-21T13:00:00+09:00',
    longlist,
  }
}

function candidateRow(overrides: Partial<CandidateRowView> = {}): CandidateRowView {
  return {
    ticker: '2331',
    name: 'ALSOK',
    sector_33: 'サービス業',
    market_cap_oku: null,
    avg_turnover_oku: null,
    per_trailing: null,
    normalized_per_3fy: null,
    per_forward: null,
    pbr: null,
    ev_ebitda: null,
    p_s: null,
    pcfr: null,
    dividend_yield: null,
    er_annual: 0.108,
    er_reversion_annual: 0.011,
    er_carry_annual: 0.098,
    net_cash_to_market_cap: null,
    fcf_yield: null,
    ocf_yield: null,
    equity_ratio: null,
    sales_yoy: null,
    operating_profit_yoy: null,
    sector_relative_strength_percentile: null,
    price_change_20d: null,
    gap_from_52w_low: null,
    next_earnings_date: '2026-08-06',
    data_quality_flags: [],
    portfolio_state: 'unheld',
    has_research: false,
    fair_value_anchor_yen: null,
    fair_value_gap_pct: null,
    ...overrides,
  }
}

describe('buildShortlistComparison', () => {
  it('orders selected candidates by the provisional rank rather than the published order', () => {
    const rows = buildShortlistComparison(
      shortlist([
        entry({ ticker: '2331', rank: 2 }),
        entry({ ticker: '0001', rank: 1 }),
        entry({ ticker: '0002', decision: 'rejected', rank: null, narrative: null }),
      ]),
      null,
      [],
    )

    expect(rows.map((item) => item.ticker)).toEqual(['0001', '2331'])
  })

  it('keeps unranked entries readable by placing them after the ranked ones in published order', () => {
    const rows = buildShortlistComparison(
      shortlist([
        entry({ ticker: '0001', rank: null }),
        entry({ ticker: '0002', rank: null }),
        entry({ ticker: '2331', rank: 1 }),
      ]),
      null,
      [],
    )

    expect(rows.map((item) => item.ticker)).toEqual(['2331', '0001', '0002'])
  })

  it('joins the machine coordinates by ticker and leaves them blank when the run is unavailable', () => {
    const rows = buildShortlistComparison(
      shortlist([entry({ ticker: '2331', rank: 1 }), entry({ ticker: '0001', rank: 2 })]),
      selection([longlistEntry()]),
      [candidateRow()],
    )

    expect(rows[0].fairValueGapPct).toBe(25)
    expect(rows[0].machineRank).toBe(3)
    expect(rows[0].erCarryAnnual).toBe(0.098)
    expect(rows[0].name).toBe('ALSOK')
    expect(rows[1].fairValueGapPct).toBeNull()
    expect(rows[1].machineRank).toBeNull()
    expect(rows[1].name).toBe('0001')
  })

  it('falls back to the candidate row fair value when the ticker left the longlist', () => {
    const rows = buildShortlistComparison(
      shortlist([entry({ ticker: '2331', rank: 1 })]),
      selection([]),
      [candidateRow({ fair_value_gap_pct: 12.5 })],
    )

    expect(rows[0].fairValueGapPct).toBe(12.5)
  })

  it('carries the risk-reward judgment and blanks it for entries published without one', () => {
    const rows = buildShortlistComparison(
      shortlist([
        entry({ ticker: '2331', rank: 1 }),
        entry({
          ticker: '0001',
          rank: 2,
          narrative: narrative({ rr: null, upside: null, catalyst_date: null }),
        }),
      ]),
      null,
      [],
    )

    expect(rows[0].rr).toBe('下値が資産で支えられ上値は倍近い')
    expect(rows[0].catalystDate).toBe('2026-08-06')
    expect(rows[1].rr).toBeNull()
    expect(rows[1].upside).toBeNull()
    expect(rows[1].catalystDate).toBeNull()
  })
})

describe('rankDivergence', () => {
  it('reports how far the human ordering departs from the machine ordering', () => {
    const [row] = buildShortlistComparison(
      shortlist([entry({ ticker: '2331', rank: 1 })]),
      selection([longlistEntry({ rank: 3 })]),
      [candidateRow()],
    )

    expect(rankDivergence(row)).toBe(2)
  })

  it('withholds the divergence when either ordering is unavailable', () => {
    const [unranked] = buildShortlistComparison(
      shortlist([entry({ ticker: '2331', rank: null })]),
      selection([longlistEntry({ rank: 3 })]),
      [],
    )
    const [unlisted] = buildShortlistComparison(
      shortlist([entry({ ticker: '2331', rank: 1 })]),
      selection([]),
      [],
    )

    expect(rankDivergence(unranked)).toBeNull()
    expect(rankDivergence(unlisted)).toBeNull()
  })
})

describe('summarize', () => {
  it('keeps short text intact and elides longer text to the requested length', () => {
    expect(summarize('短い判断')).toBe('短い判断')
    expect(summarize('あ'.repeat(50))).toBe(`${'あ'.repeat(43)}…`)
    expect(summarize(null)).toBeNull()
  })

  it('counts code points so a surrogate pair is not split in half', () => {
    expect(summarize('𠮟'.repeat(6), 4)).toBe('𠮟𠮟𠮟…')
  })
})
