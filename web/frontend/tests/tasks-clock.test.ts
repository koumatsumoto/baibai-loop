import { afterEach, expect, it, vi } from 'vitest'

import { useJstToday } from '../src/lib/use-jst-today'

const hooks = vi.hoisted(() => ({
  setToday: vi.fn(),
  effects: [] as (() => () => void)[],
}))
vi.mock('react', () => ({
  useState: (initial: () => string) => [initial(), hooks.setToday],
  useEffect: (setup: () => () => void) => { hooks.effects.push(setup) },
}))

afterEach(() => {
  vi.useRealTimers()
  hooks.setToday.mockReset()
  hooks.effects = []
})

it('updates current-date labels once at JST midnight and cancels the timer on unmount', () => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-10-14T14:59:59.900Z'))
  expect(useJstToday()).toBe('2026-10-14')
  const cleanup = hooks.effects[0]()
  vi.advanceTimersByTime(99)
  expect(hooks.setToday).not.toHaveBeenCalled()
  vi.advanceTimersByTime(1)
  expect(hooks.setToday).toHaveBeenCalledTimes(1)
  expect(hooks.setToday).toHaveBeenCalledWith('2026-10-15')
  cleanup()

  expect(useJstToday()).toBe('2026-10-15')
  const nextCleanup = hooks.effects[1]()
  expect(vi.getTimerCount()).toBe(1)
  nextCleanup()
  expect(vi.getTimerCount()).toBe(0)
  vi.advanceTimersByTime(86_400_000)
  expect(hooks.setToday).toHaveBeenCalledTimes(1)
})
