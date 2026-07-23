import { describe, expect, it } from 'vitest'

import { seriesWindowSummary } from '../src/lib/macro'

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
