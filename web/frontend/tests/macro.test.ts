import { describe, expect, it } from 'vitest'

import type { MacroGroupView, MacroReadingSeriesView, MacroReadingView, MacroSeriesFetchHealthView, MacroSeriesView } from '../src/api/types'
import { buildIndicatorGroups, filterIndicatorGroups, macroSeriesHistoryUrl, readingStatistics, seriesWindowSummary, statisticName, summarizeIndicators, transformSeriesHistory } from '../src/lib/macro'

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

function panelSeries(overrides: Partial<MacroSeriesView> = {}): MacroSeriesView {
  return {
    series_id: 'us.cpi_yoy',
    label: 'US CPI YoY',
    name: 'US CPI YoY',
    unit: 'percent',
    tradingview_symbol: null,
    points: [],
    ...overrides,
  }
}

function fetchRun(series_id: string, status: string): MacroSeriesFetchHealthView {
  return {
    series_id,
    status,
    finished_at: '2026-07-25T02:38:50+00:00',
    record_count: status === 'ok' ? 12 : 0,
    error_message: status === 'ok' ? null : 'navigation timed out',
  }
}

function readingView(overrides: Partial<MacroReadingView> = {}): MacroReadingView {
  return {
    asof: '2026-07-27',
    rules_revision: 'v6',
    series: [],
    fetch_health: [],
    ...overrides,
  }
}

function panelGroup(title: string, series: readonly MacroSeriesView[]): MacroGroupView {
  return { title, series: [...series] }
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

describe('lazy series history', () => {
  it('has no history request until a series is opened', () => {
    expect(macroSeriesHistoryUrl(null)).toBeNull()
    expect(macroSeriesHistoryUrl('us.10y')).toBe('/api/macro/series/us.10y')
  })

  it('filters and aggregates one daily history in the browser', () => {
    const series = panelSeries({
      points: [
        { observed_at: '2024-12-31', value: 1 },
        { observed_at: '2025-01-02', value: 2 },
        { observed_at: '2025-01-31', value: 3 },
        { observed_at: '2026-01-31', value: 4 },
      ],
    })

    expect(transformSeriesHistory(series, '1y', 'monthly').points).toEqual([
      { observed_at: '2025-01-31', value: 3 },
      { observed_at: '2026-01-31', value: 4 },
    ])
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

describe('buildIndicatorGroups', () => {
  it('keeps the panel groups and their order, joining each series to its reading', () => {
    const groups = buildIndicatorGroups(
      [
        panelGroup('金利・金融政策', [panelSeries({ series_id: 'us.10y' }), panelSeries({ series_id: 'jp.2y' })]),
        panelGroup('インフレ・賃金', [panelSeries({ series_id: 'us.cpi.core' })]),
      ],
      readingView({ series: [readingSeries({ series_id: 'jp.2y', latest_value: 1.1 })] }),
    )

    expect(groups.map((group) => group.title)).toEqual(['金利・金融政策', 'インフレ・賃金'])
    expect(groups[0].rows.map((row) => row.series.series_id)).toEqual(['us.10y', 'jp.2y'])
    expect(groups[0].rows[1].reading?.latest_value).toBe(1.1)
  })

  it('keeps a row whose series has no reading yet instead of dropping it', () => {
    const groups = buildIndicatorGroups([panelGroup('金利', [panelSeries({ series_id: 'jp.30y' })])], readingView())

    expect(groups[0].rows).toHaveLength(1)
    expect(groups[0].rows[0].reading).toBeNull()
    expect(groups[0].rows[0].statuses).toEqual([])
  })

  it('renders every row with blank statistics when the reading is unavailable', () => {
    const groups = buildIndicatorGroups([panelGroup('金利', [panelSeries({ series_id: 'us.10y' })])], null)

    expect(groups[0].rows[0].reading).toBeNull()
    expect(groups[0].rows[0].failedFetch).toBeNull()
  })

  it('marks a row with every state it satisfies at once', () => {
    const groups = buildIndicatorGroups(
      [panelGroup('金利', [panelSeries({ series_id: 'us.10y' })])],
      readingView({
        series: [readingSeries({ series_id: 'us.10y', stale: true, insufficient_history: true, z_score: 4 })],
        fetch_health: [fetchRun('us.10y', 'failed')],
      }),
    )

    // A withheld z-score cannot be an edge, so insufficient history keeps 'extreme' off.
    expect(groups[0].rows[0].statuses).toEqual(['fetch-failed', 'stale', 'insufficient-history'])
  })

  it('attaches only a failed acquisition attempt to its row', () => {
    const groups = buildIndicatorGroups(
      [panelGroup('金利', [panelSeries({ series_id: 'us.10y' }), panelSeries({ series_id: 'jp.2y' })])],
      readingView({ fetch_health: [fetchRun('us.10y', 'ok'), fetchRun('jp.2y', 'failed')] }),
    )

    expect(groups[0].rows[0].failedFetch).toBeNull()
    expect(groups[0].rows[0].statuses).toEqual([])
    expect(groups[0].rows[1].failedFetch?.error_message).toBe('navigation timed out')
  })

  it('counts a z-score at the threshold as an edge and one just inside it as normal', () => {
    const groups = buildIndicatorGroups(
      [panelGroup('金利', [panelSeries({ series_id: 'at' }), panelSeries({ series_id: 'at_negative' }), panelSeries({ series_id: 'inside' })])],
      readingView({
        series: [
          readingSeries({ series_id: 'at', z_score: 3 }),
          readingSeries({ series_id: 'at_negative', z_score: -3 }),
          readingSeries({ series_id: 'inside', z_score: 2.99 }),
        ],
      }),
    )

    expect(groups[0].rows.map((row) => row.statuses)).toEqual([['extreme'], ['extreme'], []])
  })
})

describe('summarizeIndicators', () => {
  it('counts every series and every state, uncapped', () => {
    const extremes = Array.from({ length: 12 }, (_, index) => `s${index}`)
    const groups = buildIndicatorGroups(
      [
        panelGroup('金利', extremes.map((series_id) => panelSeries({ series_id }))),
        panelGroup('為替', [panelSeries({ series_id: 'fx.usdjpy' })]),
      ],
      readingView({
        series: [
          ...extremes.map((series_id) => readingSeries({ series_id, z_score: 3.5 })),
          readingSeries({ series_id: 'fx.usdjpy', stale: true }),
        ],
        fetch_health: [fetchRun('fx.usdjpy', 'failed')],
      }),
    )

    expect(summarizeIndicators(groups)).toEqual({
      seriesCount: 13,
      counts: { 'fetch-failed': 1, stale: 1, 'insufficient-history': 0, extreme: 12 },
    })
  })

  it('counts the panel rows even with no reading at all', () => {
    const groups = buildIndicatorGroups([panelGroup('金利', [panelSeries({ series_id: 'us.10y' })])], null)

    expect(summarizeIndicators(groups).seriesCount).toBe(1)
  })
})

describe('filterIndicatorGroups', () => {
  const groups = buildIndicatorGroups(
    [
      panelGroup('金利・金融政策', [panelSeries({ series_id: 'us.10y', name: '米10Y利回り', label: '米10Y利回り' }), panelSeries({ series_id: 'jp.policy_rate', name: '日本政策金利', label: '日本政策金利' })]),
      panelGroup('為替', [panelSeries({ series_id: 'fx.usdjpy', name: 'ドル円', label: 'ドル円' })]),
    ],
    readingView({
      series: [readingSeries({ series_id: 'us.10y' }), readingSeries({ series_id: 'jp.policy_rate', stale: true }), readingSeries({ series_id: 'fx.usdjpy', z_score: 3.4 })],
    }),
  )

  it('returns the groups unchanged when nothing is asked for', () => {
    expect(filterIndicatorGroups(groups, { query: '  ', statuses: new Set() })).toBe(groups)
  })

  it('matches a query against the series id', () => {
    const result = filterIndicatorGroups(groups, { query: 'JP.POLICY', statuses: new Set() })
    expect(result.map((group) => group.title)).toEqual(['金利・金融政策'])
    expect(result[0].rows.map((row) => row.series.series_id)).toEqual(['jp.policy_rate'])
  })

  it('matches a query against the series name', () => {
    const result = filterIndicatorGroups(groups, { query: 'ドル円', statuses: new Set() })
    expect(result.map((group) => group.title)).toEqual(['為替'])
  })

  it('drops a group the filter empties instead of leaving a bare heading', () => {
    const result = filterIndicatorGroups(groups, { query: '', statuses: new Set(['stale'] as const) })
    expect(result).toHaveLength(1)
    expect(result[0].rows.map((row) => row.series.series_id)).toEqual(['jp.policy_rate'])
  })

  it('reads several selected states as a union', () => {
    const result = filterIndicatorGroups(groups, { query: '', statuses: new Set(['stale', 'extreme'] as const) })
    expect(result.flatMap((group) => group.rows).map((row) => row.series.series_id)).toEqual(['jp.policy_rate', 'fx.usdjpy'])
  })

  it('applies the query and the states together', () => {
    expect(filterIndicatorGroups(groups, { query: 'ドル円', statuses: new Set(['stale'] as const) })).toEqual([])
  })
})
