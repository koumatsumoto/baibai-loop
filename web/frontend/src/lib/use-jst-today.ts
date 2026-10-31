import { useEffect, useState } from 'react'

import { jstCalendarDate } from './format'

/** Refresh calendar labels at JST midnight without refetching saved facts. */
export function useJstToday(): string {
  const [today, setToday] = useState(() => jstCalendarDate(new Date().toISOString()))
  useEffect(() => {
    const nextMidnight = Date.parse(`${today}T00:00:00+09:00`) + 86_400_000
    const timer = setTimeout(() => {
      setToday(jstCalendarDate(new Date().toISOString()))
    }, Math.max(0, nextMidnight - Date.now()))
    return () => clearTimeout(timer)
  }, [today])
  return today
}
