import { describe, expect, it } from 'vitest'

import { ACTIONS_URL, NAV_TABS } from '../src/lib/nav'

function activeLabels(pathname: string): string[] {
  return NAV_TABS.filter((tab) => tab.match(pathname)).map((tab) => tab.label)
}

describe('NAV_TABS active matching', () => {
  it('activates exactly one tab per route', () => {
    for (const pathname of ['/', '/macro', '/macro/reports/x', '/stocks', '/research-triage', '/stocks/capital-allocation-assessments/x', '/securities/2331']) {
      expect(activeLabels(pathname)).toHaveLength(1)
    }
  })

  it('keeps Dashboard exact so it does not match sub-routes', () => {
    expect(activeLabels('/')).toEqual(['Dashboard'])
    expect(activeLabels('/macro')).toEqual(['Macro'])
  })

  it('keeps Macro active on the report detail route', () => {
    expect(activeLabels('/macro/reports/macro-context-2026-07-01')).toEqual(['Macro'])
  })

  it('keeps Stocks active on triage, allocation assessment and security detail routes', () => {
    expect(activeLabels('/research-triage')).toEqual(['Stocks'])
    expect(activeLabels('/stocks/capital-allocation-assessments/capital-allocation-assessment-20260728-cycle')).toEqual(['Stocks'])
    expect(activeLabels('/securities/2331')).toEqual(['Stocks'])
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
