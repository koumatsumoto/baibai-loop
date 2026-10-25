import { describe, expect, it } from 'vitest'

import { ACTIONS_URL, NAV_TABS } from '../src/lib/nav'

function activeLabels(pathname: string): string[] {
  return NAV_TABS.filter((tab) => tab.match(pathname)).map((tab) => tab.label)
}

describe('NAV_TABS active matching', () => {
  it('activates exactly one tab per route', () => {
    for (const pathname of ['/', '/tasks']) {
      expect(activeLabels(pathname)).toHaveLength(1)
    }
  })

  it('keeps Dashboard exact so it does not match sub-routes', () => {
    expect(activeLabels('/')).toEqual(['Dashboard'])
    expect(activeLabels('/market-context')).toEqual([])
  })

  it('does not retain the removed market route', () => {
    expect(activeLabels('/market-context/assessments/macro-context-2026-07-01')).toEqual([])
  })

  it('does not retain removed research routes', () => {
    expect(activeLabels('/stocks/investment-proposals/investment-proposal-original')).toEqual([])
    expect(activeLabels('/stocks/stock-assessments/stock-assessment-original')).toEqual([])
    expect(activeLabels('/securities/2331')).toEqual([])
  })

  it('keeps Tasks isolated from Dashboard', () => {
    expect(activeLabels('/tasks')).toEqual(['Tasks'])
    expect(activeLabels('/')).toEqual(['Dashboard'])
  })

  it('leaves every tab inactive on an unknown route', () => {
    expect(activeLabels('/nowhere')).toEqual([])
  })
})

describe('ACTIONS_URL', () => {
  it('points at the daily batch workflow over https', () => {
    expect(ACTIONS_URL.startsWith('https://github.com/')).toBe(true)
    expect(ACTIONS_URL).toContain('cloud-daily-batch.yml')
  })
})
