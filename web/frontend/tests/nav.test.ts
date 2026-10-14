import { describe, expect, it } from 'vitest'

import { ACTIONS_URL, NAV_TABS } from '../src/lib/nav'

function activeLabels(pathname: string): string[] {
  return NAV_TABS.filter((tab) => tab.match(pathname)).map((tab) => tab.label)
}

describe('NAV_TABS active matching', () => {
  it('activates exactly one tab per route', () => {
    for (const pathname of ['/', '/market-context', '/market-context/assessments/x', '/stocks', '/stocks/explore', '/stocks/capital-allocation-assessments/x', '/securities/2331', '/tasks']) {
      expect(activeLabels(pathname)).toHaveLength(1)
    }
  })

  it('keeps Dashboard exact so it does not match sub-routes', () => {
    expect(activeLabels('/')).toEqual(['Dashboard'])
    expect(activeLabels('/market-context')).toEqual(['調査・提案'])
  })

  it('keeps Market Context active on the report detail route', () => {
    expect(activeLabels('/market-context/assessments/macro-context-2026-07-01')).toEqual(['調査・提案'])
  })

  it('keeps Stocks active on triage, allocation assessment and security detail routes', () => {
    expect(activeLabels('/stocks/capital-allocation-assessments/capital-allocation-assessment-20260728-cycle')).toEqual(['調査・提案'])
    expect(activeLabels('/securities/2331')).toEqual(['調査・提案'])
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
