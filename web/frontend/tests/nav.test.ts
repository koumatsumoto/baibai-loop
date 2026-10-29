import { describe, expect, it } from 'vitest'

import { ACTIONS_URL, NAV_TABS } from '../src/lib/nav'

function activeLabels(pathname: string): string[] {
  return NAV_TABS.filter((tab) => tab.match(pathname)).map((tab) => tab.label)
}

describe('NAV_TABS active matching', () => {
  it.each<[string, string[]]>([
    ['/', ['Dashboard']],
    ['/tasks', ['Tasks']],
    ['/proposal', ['売買提案']],
    ['/proposal/unknown', []],
    ['/nowhere', []],
    ['/tasks/unknown', []],
  ])('selects only the current page for %s', (pathname, labels) => {
    expect(activeLabels(pathname)).toEqual(labels)
  })
})

describe('ACTIONS_URL', () => {
  it('points at the daily batch workflow over https', () => {
    expect(ACTIONS_URL.startsWith('https://github.com/')).toBe(true)
    expect(ACTIONS_URL).toContain('cloud-daily-batch.yml')
  })
})
