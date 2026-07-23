import type { MacroPointView } from '../api/types'

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
