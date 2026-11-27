import { describe, expect, it } from 'vitest'

import { pnlTone } from '../src/lib/portfolio'

describe('pnlTone', () => {
  // Monetary gains/losses use a different palette from other directional metrics.
  it.each([
    [12_000, 'text-profit'],
    [-12_000, 'text-loss'],
    [0, 'text-muted-foreground'],
    [null, 'text-muted-foreground'],
  ])('paints %s as %s', (value, expected) => {
    expect(pnlTone(value)).toBe(expected)
  })
})
