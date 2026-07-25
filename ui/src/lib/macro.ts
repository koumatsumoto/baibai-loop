import type { MacroPointView, MacroReadingSeriesView, MacroSeriesFetchHealthView } from '../api/types'

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

// How many of the most extreme series the panel lists. Every z-score is already in the
// table, so this list exists to draw attention, not to be complete.
export const EXTREME_LIST_LIMIT = 10

export interface ReadingHealth {
  // Series whose last acquisition attempt failed. The store still serves the previous
  // values, so this is the only immediate sign that a source stopped answering.
  readonly failedFetches: readonly MacroSeriesFetchHealthView[]
  // Observation older than the series' staleness threshold: suspect a silent provider stop.
  readonly stale: readonly MacroReadingSeriesView[]
  readonly insufficientHistory: readonly MacroReadingSeriesView[]
  // Nothing stands between the reader and the statistics.
  readonly clear: boolean
}

// Health is about acquisition only. A failed fetch, an observation past its staleness
// threshold and a window the history does not fill each break what a percentile means, so
// they belong next to the numbers. An extreme z-score breaks nothing — it is the reading's
// own answer to "where are we" — and is classified separately.
export function readingHealth(
  series: readonly MacroReadingSeriesView[],
  fetchHealth: readonly MacroSeriesFetchHealthView[],
): ReadingHealth {
  const failedFetches = fetchHealth.filter((item) => item.status !== 'ok')
  const stale = series.filter((item) => item.stale)
  const insufficientHistory = series.filter((item) => item.insufficient_history)
  return {
    failedFetches,
    stale,
    insufficientHistory,
    clear: failedFetches.length === 0 && stale.length === 0 && insufficientHistory.length === 0,
  }
}

export interface ReadingExtreme {
  readonly series: MacroReadingSeriesView
  readonly zScore: number
}

// Series sitting at the edge of their own distribution, furthest first. Being at the edge
// is what the reading is for — a policy rate at its ten-year high reads as +3.8 z because
// it is at its ten-year high — so this is a reading, not a doubt about the value. Whether
// an edge is instead a wrong number is settled against primary sources by the report.
export function readingExtremes(
  series: readonly MacroReadingSeriesView[],
): readonly ReadingExtreme[] {
  return series
    // A withheld z-score cannot be extreme, so insufficient history takes precedence.
    .map((item) => ({ series: item, zScore: readingStatistics(item).zScore }))
    .filter(
      (item): item is ReadingExtreme =>
        item.zScore !== null && Math.abs(item.zScore) >= EXTREME_Z_SCORE,
    )
    .sort((left, right) => Math.abs(right.zScore) - Math.abs(left.zScore))
    .slice(0, EXTREME_LIST_LIMIT)
}

export interface ReadingCategory {
  readonly category: string
  readonly series: readonly MacroReadingSeriesView[]
}

// Series bucketed by category so 100+ readings stay scannable. Categories are ordered by
// name and series keep the reading's own order, so the same as-of date always renders the
// same panel and a category added to the registry lands in a predictable place.
export function readingCategories(series: readonly MacroReadingSeriesView[]): readonly ReadingCategory[] {
  const grouped = new Map<string, MacroReadingSeriesView[]>()
  for (const item of series) {
    const bucket = grouped.get(item.category)
    if (bucket === undefined) grouped.set(item.category, [item])
    else bucket.push(item)
  }
  return [...grouped]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([category, items]) => ({ category, series: items }))
}

