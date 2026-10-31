import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { TaskView, TasksView } from '../src/api/types'
import { EventsCard, NextTaskCard, TaskList } from '../src/pages/TasksPage'

describe('Tasks partial failure', () => {
  it('does not describe unavailable upcoming events as zero events', () => {
    const markup = renderToStaticMarkup(createElement(EventsCard, {
      events: [], today: '2026-10-25',
      ledgerError: 'invalid ledger',
    }))

    expect(markup).toContain('イベント情報が不完全です')
    expect(markup).toContain('不完全')
    expect(markup).not.toContain('0 件')
    expect(markup).not.toContain('イベントはありません')
  })
})


it('用件と一覧は生成日時と別の期限を表示し、期限超過を保持する', () => {
  const task: TaskView = { task_id: 'synthetic-task', title: '合成の確認用件', kind: 'follow-up', status: 'open', ticker: null, due_date: '2026-09-30', event_label: null, event_date: null }
  const data: TasksView = { generated_at: '2026-10-13T06:49:00Z', tasks_exist: true, open_tasks: [task], next_task: task, upcoming_events: [], earnings_calendar_available: null, earnings_calendar_error: null, ledger_error: null }
  for (const node of [createElement(NextTaskCard, { task, today: '2026-10-13' }), createElement(TaskList, { data, today: '2026-10-13' })]) {
    const markup = renderToStaticMarkup(node)
    expect(markup).toMatch(/<time[^>]*dateTime="2026-09-30"[^>]*>期限 /)
    expect(markup).toContain('期限超過')
  }
})


it('does not turn an unavailable earnings schedule into no events or hide reservation expiries', () => {
  const markup = renderToStaticMarkup(createElement(EventsCard, {
    events: [{ event_date: '2026-10-26', kind: 'reservation_expiry', ticker: '1234', label: '1234' }],
    ledgerError: null, earningsCalendarAvailable: false, today: '2026-10-25',
    earningsCalendarError: 'corrupt JPX earnings calendar snapshot: row count mismatch',
  }))
  expect(markup).toContain('決算予定を確認できません')
  expect(markup).toContain('不完全')
  expect(markup).toContain('予約期限')
  expect(markup).toContain('1234')
  expect(markup).toContain('row count mismatch')
  const empty = renderToStaticMarkup(createElement(EventsCard, {
    events: [], ledgerError: null, earningsCalendarAvailable: false, today: '2026-10-25',
  }))
  expect(empty).not.toContain('イベントはありません')
  expect(empty).not.toContain('0 件')
})


it('updates task overdue badges when the displayed JST date changes', () => {
  const task: TaskView = { task_id: 'due', title: '要確認', kind: 'follow-up', status: 'open', ticker: null, due_date: '2026-10-15', event_label: null, event_date: null }
  const data: TasksView = { generated_at: '2026-10-14T22:43:00+09:00', tasks_exist: true, open_tasks: [task], next_task: task, upcoming_events: [], earnings_calendar_available: null, earnings_calendar_error: null, ledger_error: null }
  for (const today of ['2026-10-15', '2026-10-16']) {
    for (const node of [createElement(NextTaskCard, { task, today }), createElement(TaskList, { data, today })]) {
      const markup = renderToStaticMarkup(node)
      expect(markup.includes('期限超過')).toBe(today === '2026-10-16')
    }
  }
})

it('relabels saved upcoming events as today and past without discarding them', () => {
  const events = [{ event_date: '2026-10-15', kind: 'reservation_expiry' as const, ticker: '1234', label: '1234' }]
  for (const [today, label] of [['2026-10-15', '本日'], ['2026-10-16', '1 日前'], ['2026-10-17', '2 日前']]) {
    const markup = renderToStaticMarkup(createElement(EventsCard, { events, ledgerError: null, today }))
    expect(markup).toContain(label)
    expect(markup).toContain('1234')
    expect(markup).not.toContain('あと 1 日')
    expect(markup).not.toContain('あと -')
  }
})
