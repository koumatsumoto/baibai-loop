import { describe, expect, it } from 'vitest'

import type { MacroReadingSeriesView } from '../src/api/types'
import { EXTREME_LIST_LIMIT, readingCategories, readingExtremes, readingHealth, readingStatistics, seriesWindowSummary, statisticName } from '../src/lib/macro'

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
    next_print_estimate: '2026-08-15',
    print_due_in_days: 22,
    window_years: 10,
    window_observations: 120,
    expected_observations: 120,
    insufficient_history: false,
    statistic: 'level',
    statistic_unit: 'percent',
    statistic_value: 2.4,
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

  it('withholds the statistics of a year-on-year series with no comparable observation', () => {
    const stats = readingStatistics(readingSeries({ statistic: 'yoy', statistic_value: null, percentile: 0.5, z_score: 1 }))
    expect(stats.percentilePct).toBeNull()
    expect(stats.zScore).toBeNull()
    expect(stats.withheldNote).toBe('前年比を取れない')
  })
})

describe('statisticName', () => {
  it('names the statistic the percentile ranks', () => {
    expect(statisticName('level')).toBe('水準')
    expect(statisticName('yoy')).toBe('前年比')
  })

  it('falls back to the raw name so an unknown statistic is not displayed as a level', () => {
    expect(statisticName('mom')).toBe('mom')
  })
})

describe('readingHealth', () => {
  const run = (series_id: string, status: string) => ({
    series_id,
    status,
    finished_at: '2026-07-25T02:38:50+00:00',
    record_count: status === 'ok' ? 12 : 0,
    error_message: status === 'ok' ? null : 'navigation timed out',
  })

  it('sorts each series into the acquisition classification it belongs to', () => {
    const staleSeries = readingSeries({ series_id: 'a', stale: true })
    const youngSeries = readingSeries({ series_id: 'b', insufficient_history: true })
    const health = readingHealth([staleSeries, youngSeries, readingSeries({ series_id: 'd' })], [run('e', 'failed')])
    expect(health.stale.map((item) => item.series_id)).toEqual(['a'])
    expect(health.insufficientHistory.map((item) => item.series_id)).toEqual(['b'])
    expect(health.failedFetches.map((item) => item.series_id)).toEqual(['e'])
    expect(health.clear).toBe(false)
  })

  it('keeps an extreme z-score out of health, which is about acquisition only', () => {
    const health = readingHealth([readingSeries({ series_id: 'c', z_score: -3.4 })], [run('a', 'ok')])
    expect(health.stale).toEqual([])
    expect(health.insufficientHistory).toEqual([])
    expect(health.failedFetches).toEqual([])
    expect(health.clear).toBe(true)
  })

  it('lists a series in every classification it satisfies at once', () => {
    const health = readingHealth([readingSeries({ series_id: 'both', stale: true, insufficient_history: true })], [])
    expect(health.stale.map((item) => item.series_id)).toEqual(['both'])
    expect(health.insufficientHistory.map((item) => item.series_id)).toEqual(['both'])
  })

  it('keeps only the series whose last acquisition attempt failed', () => {
    const health = readingHealth([], [run('a', 'ok'), run('b', 'failed'), run('c', 'ok')])
    expect(health.failedFetches.map((item) => item.series_id)).toEqual(['b'])
  })
})

describe('readingExtremes', () => {
  it('lists the series at the edge of their distribution, furthest first', () => {
    const extremes = readingExtremes([
      readingSeries({ series_id: 'mild', z_score: 3.1 }),
      readingSeries({ series_id: 'normal', z_score: 1.2 }),
      readingSeries({ series_id: 'furthest', z_score: -3.9 }),
    ])
    expect(extremes.map((item) => item.series.series_id)).toEqual(['furthest', 'mild'])
    expect(extremes[0].zScore).toBe(-3.9)
  })

  it('counts a z-score at the threshold as an edge and one just inside it as normal', () => {
    const extremes = readingExtremes([
      readingSeries({ series_id: 'at', z_score: 3 }),
      readingSeries({ series_id: 'at_negative', z_score: -3 }),
      readingSeries({ series_id: 'inside', z_score: 2.99 }),
      readingSeries({ series_id: 'inside_negative', z_score: -2.99 }),
    ])
    expect(extremes.map((item) => item.series.series_id)).toEqual(['at', 'at_negative'])
  })

  it('never calls a withheld z-score an edge', () => {
    const extremes = readingExtremes([
      readingSeries({ insufficient_history: true, z_score: 5 }),
      readingSeries({ z_score: null }),
      readingSeries({ statistic: 'yoy', statistic_value: null, z_score: 4 }),
    ])
    expect(extremes).toEqual([])
  })

  it('caps the list so the panel draws attention instead of repeating the table', () => {
    const many = Array.from({ length: EXTREME_LIST_LIMIT + 3 }, (_, index) =>
      readingSeries({ series_id: `s${index}`, z_score: 3 + index / 100 }),
    )
    expect(readingExtremes(many)).toHaveLength(EXTREME_LIST_LIMIT)
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
