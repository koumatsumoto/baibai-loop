import type {
  MacroGroupView,
  MacroPointView,
  MacroReadingSeriesView,
  MacroReadingView,
  MacroSeriesFetchHealthView,
  MacroSeriesView,
} from '../api/types'

export interface SeriesWindowSummary {
  // Most recent observation in the window, or null when there are no points.
  readonly latest: number | null
  // Signed change from the first to the last point in the window. null when
  // fewer than two points exist (no change can be stated over a single point).
  readonly delta: number | null
}

// At-a-glance summary for a sparkline: latest level plus the change across the
// displayed window. Direction is not interpreted as good/bad here — macro series
// have no universal sign — so callers show a neutral arrow, not a semantic color.
export function seriesWindowSummary(points: readonly MacroPointView[]): SeriesWindowSummary {
  if (points.length === 0) return { latest: null, delta: null }
  const latest = points[points.length - 1].value
  const delta = points.length >= 2 ? latest - points[0].value : null
  return { latest, delta }
}

export interface ReadingStatistics {
  // Percentile rescaled to 0–100, or null when the reading withholds it.
  readonly percentilePct: number | null
  readonly zScore: number | null
  // Why the statistics are absent, for display next to the blanks; null when present.
  readonly withheldNote: string | null
}

// The statistics of one series, as the reading permits them to be shown. A series whose
// history does not fill the effective window has no historical position at all, so the
// panel shows blanks and says why rather than ranking it inside its own short life; a
// year-on-year series with no observation a year back has no current statistic to rank.
export function readingStatistics(series: MacroReadingSeriesView): ReadingStatistics {
  if (series.insufficient_history) {
    return { percentilePct: null, zScore: null, withheldNote: `履歴不足（${series.window_years}y 窓）` }
  }
  if (series.statistic_value === null) {
    return { percentilePct: null, zScore: null, withheldNote: `${statisticName(series.statistic)}を取れない` }
  }
  return {
    percentilePct: series.percentile === null ? null : series.percentile * 100,
    zScore: series.z_score,
    withheldNote: null,
  }
}

// What percentile and z rank. Shown per row because a level percentile and a
// year-on-year one answer different questions and share the same column.
const STATISTIC_NAME: Readonly<Record<string, string>> = { level: '水準', yoy: '前年比' }

export function statisticName(statistic: string): string {
  return STATISTIC_NAME[statistic] ?? statistic
}

// A |z| at or beyond this puts a series at the edge of its own distribution.
export const EXTREME_Z_SCORE = 3

// The states a row can carry. The first three are acquisition problems, and each breaks
// what a percentile means. The fourth is the reading's own answer to "where are we" — a
// policy rate at its ten-year high reads as +3.8 z because it is at its ten-year high —
// so it is counted apart from the other three and never presented as a defect.
export type MacroIndicatorStatus = 'fetch-failed' | 'stale' | 'insufficient-history' | 'extreme'

export const INDICATOR_STATUSES: readonly MacroIndicatorStatus[] = [
  'fetch-failed',
  'stale',
  'insufficient-history',
  'extreme',
]

export const INDICATOR_STATUS_LABEL: Readonly<Record<MacroIndicatorStatus, string>> = {
  'fetch-failed': '取得失敗',
  stale: 'stale',
  'insufficient-history': '履歴不足',
  extreme: '分布の端',
}

export interface MacroIndicatorRow {
  // Chart side: label, unit, TradingView symbol and the points of the displayed window.
  readonly series: MacroSeriesView
  // Reading side: where the series stands and how old it is. null while the reading is
  // unavailable, or for a series the registry has gained since the reading was computed.
  readonly reading: MacroReadingSeriesView | null
  // The last acquisition attempt, present only when it failed. Staleness cannot see a
  // source that has just gone silent: a monthly series stays inside its threshold for
  // weeks after its provider stops answering.
  readonly failedFetch: MacroSeriesFetchHealthView | null
  readonly statuses: readonly MacroIndicatorStatus[]
}

export interface MacroIndicatorGroup {
  readonly title: string
  readonly rows: readonly MacroIndicatorRow[]
}

// Join the indicator panel with the machine reading into the rows the Macro tab draws.
//
// Both projections cover the same registry — the panel places every registered series in
// exactly one group, the reading computes one row per registered series — so the join is
// total by construction and the panel decides the rows. The panel keeps a series the
// store has not fetched, so a row with no reading is a freshness state rather than an
// error: it renders with blank statistics instead of disappearing.
export function buildIndicatorGroups(
  groups: readonly MacroGroupView[],
  reading: MacroReadingView | null,
): readonly MacroIndicatorGroup[] {
  const readings = new Map((reading?.series ?? []).map((item) => [item.series_id, item]))
  const failures = new Map(
    (reading?.fetch_health ?? [])
      .filter((item) => item.status !== 'ok')
      .map((item) => [item.series_id, item]),
  )
  return groups.map((group) => ({
    title: group.title,
    rows: group.series.map((series) => {
      const item = readings.get(series.series_id) ?? null
      const failedFetch = failures.get(series.series_id) ?? null
      return { series, reading: item, failedFetch, statuses: rowStatuses(item, failedFetch) }
    }),
  }))
}

function rowStatuses(
  reading: MacroReadingSeriesView | null,
  failedFetch: MacroSeriesFetchHealthView | null,
): readonly MacroIndicatorStatus[] {
  const statuses: MacroIndicatorStatus[] = []
  if (failedFetch !== null) statuses.push('fetch-failed')
  if (reading === null) return statuses
  if (reading.stale) statuses.push('stale')
  if (reading.insufficient_history) statuses.push('insufficient-history')
  // A withheld z-score cannot be extreme, so insufficient history takes precedence.
  const zScore = readingStatistics(reading).zScore
  if (zScore !== null && Math.abs(zScore) >= EXTREME_Z_SCORE) statuses.push('extreme')
  return statuses
}

export interface MacroIndicatorSummary {
  readonly seriesCount: number
  readonly counts: Readonly<Record<MacroIndicatorStatus, number>>
}

// Counts for the page's summary card. Every count is a pointer into the one table below:
// the same classification filters the rows, so a non-zero count is reachable in one click.
export function summarizeIndicators(
  groups: readonly MacroIndicatorGroup[],
): MacroIndicatorSummary {
  const counts: Record<MacroIndicatorStatus, number> = {
    'fetch-failed': 0,
    stale: 0,
    'insufficient-history': 0,
    extreme: 0,
  }
  let seriesCount = 0
  for (const group of groups) {
    for (const row of group.rows) {
      seriesCount += 1
      for (const status of row.statuses) counts[status] += 1
    }
  }
  return { seriesCount, counts }
}

export interface IndicatorFilter {
  // Substring of the series name or id. Empty matches everything.
  readonly query: string
  // Selected states. Empty matches everything; several are read as a union, so picking
  // 取得失敗 and stale asks for the rows that carry either.
  readonly statuses: ReadonlySet<MacroIndicatorStatus>
}

// Narrow the table to the rows a filter asks for, dropping the groups left with none so
// the result is the answer rather than seven headings around it.
export function filterIndicatorGroups(
  groups: readonly MacroIndicatorGroup[],
  filter: IndicatorFilter,
): readonly MacroIndicatorGroup[] {
  const query = filter.query.trim().toLowerCase()
  if (query === '' && filter.statuses.size === 0) return groups
  return groups
    .map((group) => ({
      title: group.title,
      rows: group.rows.filter((row) => matchesFilter(row, query, filter.statuses)),
    }))
    .filter((group) => group.rows.length > 0)
}

function matchesFilter(
  row: MacroIndicatorRow,
  query: string,
  statuses: ReadonlySet<MacroIndicatorStatus>,
): boolean {
  if (statuses.size > 0 && !row.statuses.some((status) => statuses.has(status))) return false
  if (query === '') return true
  return (
    row.series.series_id.toLowerCase().includes(query) ||
    row.series.name.toLowerCase().includes(query) ||
    row.series.label.toLowerCase().includes(query)
  )
}
