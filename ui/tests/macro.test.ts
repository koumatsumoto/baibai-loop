import { describe, expect, it } from 'vitest'

import type { MacroReadingSeriesView } from '../src/api/types'
import { failedFetches, readingCategories, readingHealth, readingStatistics, seriesWindowSummary } from '../src/lib/macro'

function readingSeries(overrides: Partial<MacroReadingSeriesView> = {}): MacroReadingSeriesView {
  return {
    series_id: 'us.cpi_yoy',
    name: 'US CPI YoY',
    category: 'inflation',
    geography: 'us',
    frequency: 'monthly',
    unit: 'percent',
    latest_value: 2.4,
    observed_at: '2026-06-30',
    staleness_days: 24,
    stale: false,
    staleness_warn_days: 100,
    window_years: 10,
    window_observations: 120,
    insufficient_history: false,
    percentile: 0.83,
    z_score: 0.9,
    short_trend: null,
    long_trend: null,
    flags: [],
    ...overrides,
  }
}

describe('seriesWindowSummary', () => {
  it('reports null latest and delta for an empty window', () => {
    expect(seriesWindowSummary([])).toEqual({ latest: null, delta: null })
  })

  it('reports the latest value but no delta for a single point', () => {
    expect(seriesWindowSummary([{ observed_at: '2026-07-01', value: 4.5 }])).toEqual({
      latest: 4.5,
      delta: null,
    })
  })

  it('reports the signed change from first to last point', () => {
    const points = [
      { observed_at: '2026-01-01', value: 4.0 },
      { observed_at: '2026-04-01', value: 4.2 },
      { observed_at: '2026-07-01', value: 3.6 },
    ]
    const summary = seriesWindowSummary(points)
    expect(summary.latest).toBe(3.6)
    expect(summary.delta).toBeCloseTo(-0.4, 10)
  })
})

describe('readingStatistics', () => {
  it('rescales a 0-1 percentile to percent and keeps the z-score', () => {
    const stats = readingStatistics(readingSeries({ percentile: 0.83, z_score: 0.9 }))
    expect(stats.percentilePct).toBeCloseTo(83, 10)
    expect(stats.zScore).toBe(0.9)
    expect(stats.withheldNote).toBeNull()
  })

  it('rescales the 0 and 1 percentile bounds to 0% and 100%', () => {
    expect(readingStatistics(readingSeries({ percentile: 0 })).percentilePct).toBe(0)
    expect(readingStatistics(readingSeries({ percentile: 1 })).percentilePct).toBe(100)
  })

  it('withholds both statistics with the window in the note when history is insufficient', () => {
    const stats = readingStatistics(readingSeries({ insufficient_history: true, window_years: 3, percentile: 0.5, z_score: 2 }))
    expect(stats.percentilePct).toBeNull()
    expect(stats.zScore).toBeNull()
    expect(stats.withheldNote).toBe('履歴不足（3y 窓）')
  })

  it('reports a null percentile as null rather than zero percent', () => {
    expect(readingStatistics(readingSeries({ percentile: null })).percentilePct).toBeNull()
  })
})

describe('readingHealth', () => {
  it('sorts each series into the classifications it belongs to', () => {
    const staleSeries = readingSeries({ series_id: 'a', stale: true })
    const youngSeries = readingSeries({ series_id: 'b', insufficient_history: true })
    const extremeSeries = readingSeries({ series_id: 'c', z_score: -3.4 })
    const health = readingHealth([staleSeries, youngSeries, extremeSeries, readingSeries({ series_id: 'd' })])
    expect(health.stale.map((item) => item.series_id)).toEqual(['a'])
    expect(health.insufficientHistory.map((item) => item.series_id)).toEqual(['b'])
    expect(health.extremeZ.map((item) => item.series_id)).toEqual(['c'])
  })

  it('counts a z-score at the threshold as suspect and one just inside it as normal', () => {
    const health = readingHealth([
      readingSeries({ series_id: 'at', z_score: 3 }),
      readingSeries({ series_id: 'at_negative', z_score: -3 }),
      readingSeries({ series_id: 'inside', z_score: 2.99 }),
      readingSeries({ series_id: 'inside_negative', z_score: -2.99 }),
    ])
    expect(health.extremeZ.map((item) => item.series_id)).toEqual(['at', 'at_negative'])
  })

  it('never calls a withheld z-score extreme', () => {
    const health = readingHealth([readingSeries({ insufficient_history: true, z_score: 5 }), readingSeries({ z_score: null })])
    expect(health.extremeZ).toEqual([])
  })

  it('lists a series in every classification it satisfies at once', () => {
    const health = readingHealth([readingSeries({ series_id: 'both', stale: true, insufficient_history: true })])
    expect(health.stale.map((item) => item.series_id)).toEqual(['both'])
    expect(health.insufficientHistory.map((item) => item.series_id)).toEqual(['both'])
  })
})

describe('readingCategories', () => {
  it('orders categories by name and keeps the reading order inside each one', () => {
    const groups = readingCategories([
      readingSeries({ series_id: 'a', category: 'rates' }),
      readingSeries({ series_id: 'b', category: 'inflation' }),
      readingSeries({ series_id: 'c', category: 'rates' }),
    ])
    expect(groups.map((group) => group.category)).toEqual(['inflation', 'rates'])
    expect(groups[1].series.map((item) => item.series_id)).toEqual(['a', 'c'])
  })
})

describe('failedFetches', () => {
  const run = (series_id: string, status: string) => ({
    series_id,
    status,
    finished_at: '2026-07-25T02:38:50+00:00',
    record_count: status === 'ok' ? 12 : 0,
    error_message: status === 'ok' ? null : 'navigation timed out',
  })

  it('keeps only the series whose last acquisition attempt failed', () => {
    const failed = failedFetches([run('a', 'ok'), run('b', 'failed'), run('c', 'ok')])

    expect(failed.map((item) => item.series_id)).toEqual(['b'])
  })

  it('reports nothing when every series was fetched successfully', () => {
    expect(failedFetches([run('a', 'ok')])).toEqual([])
  })
})
