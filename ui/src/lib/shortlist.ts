import type {
  CandidateRowView,
  MachineSelectionView,
  SelectionLonglistEntryView,
  ShortlistEntryView,
  ShortlistView,
} from '../api/types'

// One selected candidate as the review surface compares it: the human's provisional
// ordering and risk-reward judgment beside the machine coordinates it must survive.
// Machine values are joined by ticker, never re-derived here — the read model is the
// only place that computes them.
export interface ShortlistComparisonRow {
  readonly rank: number | null
  readonly ticker: string
  readonly name: string
  readonly sector: string
  readonly ploss: string | null
  readonly rr: string | null
  readonly upside: string | null
  readonly downside: string | null
  readonly catalyst: string | null
  readonly catalystDate: string | null
  readonly machineRank: number | null
  readonly erAnnual: number | null
  readonly erReversionAnnual: number | null
  readonly erCarryAnnual: number | null
  readonly fairValueGapPct: number | null
  readonly portfolioState: CandidateRowView['portfolio_state'] | null
  readonly dataQualityFlags: readonly string[]
  readonly entry: ShortlistEntryView
  readonly longlistEntry: SelectionLonglistEntryView | null
  readonly row: CandidateRowView | null
}

// Selected candidates in the order the human ranked them. Entries without a rank
// (published before ranking became part of the judgment) keep their published order
// after the ranked ones, so an older shortlist still reads top to bottom.
export function buildShortlistComparison(
  shortlist: ShortlistView,
  selection: MachineSelectionView | null,
  rows: readonly CandidateRowView[],
): readonly ShortlistComparisonRow[] {
  const longlistByTicker = new Map(selection?.longlist.map((item) => [item.ticker, item]) ?? [])
  const rowByTicker = new Map(rows.map((item) => [item.ticker, item]))
  return shortlist.entries
    .filter((entry) => entry.decision === 'selected')
    .map((entry, index) => toComparisonRow(entry, index, longlistByTicker, rowByTicker))
    .sort(byRankThenPublishedOrder)
}

function toComparisonRow(
  entry: ShortlistEntryView,
  publishedIndex: number,
  longlistByTicker: ReadonlyMap<string, SelectionLonglistEntryView>,
  rowByTicker: ReadonlyMap<string, CandidateRowView>,
): ShortlistComparisonRow & { readonly publishedIndex: number } {
  const longlistEntry = longlistByTicker.get(entry.ticker) ?? null
  const row = rowByTicker.get(entry.ticker) ?? null
  const narrative = entry.narrative
  return {
    publishedIndex,
    rank: entry.rank,
    ticker: entry.ticker,
    name: row?.name ?? longlistEntry?.name ?? entry.ticker,
    sector: narrative?.sector_label ?? row?.sector_33 ?? '—',
    ploss: narrative?.ploss ?? null,
    rr: narrative?.rr ?? null,
    upside: narrative?.upside ?? null,
    downside: narrative?.downside ?? null,
    catalyst: narrative?.catalyst ?? null,
    catalystDate: narrative?.catalyst_date ?? null,
    machineRank: longlistEntry?.rank ?? null,
    erAnnual: row?.er_annual ?? null,
    erReversionAnnual: row?.er_reversion_annual ?? null,
    erCarryAnnual: row?.er_carry_annual ?? null,
    fairValueGapPct: longlistEntry?.fair_value_gap_pct ?? row?.fair_value_gap_pct ?? null,
    portfolioState: row?.portfolio_state ?? null,
    dataQualityFlags: row?.data_quality_flags ?? [],
    entry,
    longlistEntry,
    row,
  }
}

function byRankThenPublishedOrder(
  left: ShortlistComparisonRow & { readonly publishedIndex: number },
  right: ShortlistComparisonRow & { readonly publishedIndex: number },
): number {
  if (left.rank !== null && right.rank !== null) return left.rank - right.rank
  if (left.rank !== null) return -1
  if (right.rank !== null) return 1
  return left.publishedIndex - right.publishedIndex
}

// How far the human's ordering departs from the machine's E[r] ordering. The gate asks
// the author to justify a departure, so the surface has to make it visible rather than
// leaving the reader to compare two columns by eye.
export function rankDivergence(row: ShortlistComparisonRow): number | null {
  if (row.rank === null || row.machineRank === null) return null
  return row.machineRank - row.rank
}

// A one-line prose cell: enough to compare across rows, with the full text left to the
// candidate card below. Counting by code points keeps surrogate pairs intact.
export function summarize(text: string | null, maxLength = 44): string | null {
  if (text === null) return null
  const characters = [...text.trim()]
  if (characters.length <= maxLength) return characters.join('')
  return `${characters.slice(0, maxLength - 1).join('')}…`
}
