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

// A |z| at or beyond this is worth a second look: it comes out both for a wrong value and
// for a genuine market extreme. The reading does not decide which — the report does.
export const EXTREME_Z_SCORE = 3

export interface ReadingHealth {
  // Observation older than the series' staleness threshold: suspect a silent provider stop.
  readonly stale: readonly MacroReadingSeriesView[]
  readonly insufficientHistory: readonly MacroReadingSeriesView[]
  readonly extremeZ: readonly MacroReadingSeriesView[]
}

export function readingHealth(series: readonly MacroReadingSeriesView[]): ReadingHealth {
  return {
    stale: series.filter((item) => item.stale),
    insufficientHistory: series.filter((item) => item.insufficient_history),
    // A withheld z-score cannot be extreme, so insufficient history takes precedence.
    extremeZ: series.filter((item) => {
      const { zScore } = readingStatistics(item)
      return zScore !== null && Math.abs(zScore) >= EXTREME_Z_SCORE
    }),
  }
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

// Series whose last acquisition attempt failed. The observation store still holds the
// previous values, so the reading looks healthy while the source has already stopped
// answering — this is the only place that difference shows.
export function failedFetches(
  fetchHealth: readonly MacroSeriesFetchHealthView[],
): readonly MacroSeriesFetchHealthView[] {
  return fetchHealth.filter((item) => item.status !== 'ok')
}
