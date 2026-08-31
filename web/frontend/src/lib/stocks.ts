import type { SecurityAnalysisRowView } from '../api/types'

export const SECURITY_PAGE_SIZE = 100

export type SecuritySortKey =
  | 'ticker'
  | 'name'
  | 'sector_33'
  | 'market_cap_oku'
  | 'per_forward'
  | 'per_trailing'
  | 'normalized_per_3fy'
  | 'pbr'
  | 'dividend_yield'
  | 'net_cash_to_market_cap'
  | 'fcf_yield'
  | 'sales_yoy'
  | 'operating_profit_yoy'
  | 'er_annual'
  | 'fair_value_gap_pct'
  | 'next_earnings_date'

export interface SecurityFilters {
  query: string
  sector: string
  perMax: number | null
  pbrMax: number | null
  dividendMinPct: number | null
  heldOnly: boolean
  researchOnly: boolean
}

export interface SecuritySort {
  key: SecuritySortKey
  direction: 'asc' | 'desc'
}

export interface SecurityProjection {
  rows: SecurityAnalysisRowView[]
  filteredTotal: number
  page: number
  pageCount: number
}

export function parseOptionalNumber(value: string): number | null {
  const trimmed = value.trim()
  if (trimmed === '') return null
  const parsed = Number(trimmed)
  return Number.isFinite(parsed) ? parsed : null
}

function normalized(value: string): string {
  return value.trim().normalize('NFKC').toLocaleLowerCase('ja-JP')
}

export function filterSecurityRows(
  rows: readonly SecurityAnalysisRowView[],
  filters: SecurityFilters,
): SecurityAnalysisRowView[] {
  const query = normalized(filters.query)
  return rows.filter((row) => {
    const matchesQuery =
      query === '' ||
      normalized(row.ticker).startsWith(query) ||
      normalized(row.name ?? '').includes(query)
    return (
      matchesQuery &&
      (filters.sector === 'all' || row.sector_33 === filters.sector) &&
      (filters.perMax === null || (row.per_trailing !== null && row.per_trailing <= filters.perMax)) &&
      (filters.pbrMax === null || (row.pbr !== null && row.pbr <= filters.pbrMax)) &&
      (filters.dividendMinPct === null || (row.dividend_yield !== null && row.dividend_yield * 100 >= filters.dividendMinPct)) &&
      (!filters.heldOnly || row.portfolio_state === 'held' || row.portfolio_state === 'held_and_reserved') &&
      (!filters.researchOnly || row.has_research)
    )
  })
}

function sortValue(row: SecurityAnalysisRowView, key: SecuritySortKey): string | number | null {
  const value = row[key]
  return typeof value === 'string' || typeof value === 'number' ? value : null
}

export function sortSecurityRows(
  rows: readonly SecurityAnalysisRowView[],
  sort: SecuritySort,
): SecurityAnalysisRowView[] {
  return [...rows].sort((left, right) => {
    const leftValue = sortValue(left, sort.key)
    const rightValue = sortValue(right, sort.key)
    if (leftValue === null && rightValue === null) return left.ticker.localeCompare(right.ticker)
    if (leftValue === null) return 1
    if (rightValue === null) return -1
    const compared = typeof leftValue === 'number' && typeof rightValue === 'number'
      ? leftValue - rightValue
      : String(leftValue).localeCompare(String(rightValue), 'ja-JP')
    return (sort.direction === 'asc' ? compared : -compared) || left.ticker.localeCompare(right.ticker)
  })
}

export function projectSecurityRows(
  rows: readonly SecurityAnalysisRowView[],
  filters: SecurityFilters,
  sort: SecuritySort,
  requestedPage: number,
): SecurityProjection {
  const filtered = sortSecurityRows(filterSecurityRows(rows, filters), sort)
  const pageCount = Math.max(1, Math.ceil(filtered.length / SECURITY_PAGE_SIZE))
  const page = Math.min(Math.max(1, requestedPage), pageCount)
  const start = (page - 1) * SECURITY_PAGE_SIZE
  return {
    rows: filtered.slice(start, start + SECURITY_PAGE_SIZE),
    filteredTotal: filtered.length,
    page,
    pageCount,
  }
}

export function valuationApproachLabel(approach: string, rank: number): string {
  const labels: Readonly<Record<string, string>> = {
    'current-earnings-power': 'Current',
    'normalized-earnings-power': 'Normalized',
    'asset-value': 'Asset',
    'reinvestment-value': 'Reinvestment',
  }
  return `${labels[approach] ?? approach} #${rank}`
}
