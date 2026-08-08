import { describe, expect, it } from 'vitest'

import { elapsedLabel } from '../src/pages/SystemPage'

const NOW = Date.parse('2026-07-27T09:00:00+09:00')

describe('elapsedLabel', () => {
  it('reads sub-hour ages as within the hour', () => {
    expect(elapsedLabel('2026-07-27T08:30:00+09:00', NOW)).toBe('1 時間以内')
  })

  it('counts whole hours below a day', () => {
    expect(elapsedLabel('2026-07-26T18:30:00+09:00', NOW)).toBe('14 時間前')
  })

  it('counts whole days at and beyond a day', () => {
    expect(elapsedLabel('2026-07-24T18:30:00+09:00', NOW)).toBe('2 日前')
  })

  it('says nothing for an unparseable stamp', () => {
    expect(elapsedLabel('not-a-timestamp', NOW)).toBe('')
  })

  it('says nothing when the stamp is ahead of now', () => {
    // Clock skew between the runner and the viewer must not render "-1 日前".
    expect(elapsedLabel('2026-07-27T10:00:00+09:00', NOW)).toBe('')
  })
})
