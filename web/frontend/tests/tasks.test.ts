import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { TaskView, TasksView } from '../src/api/types'
import { EventsCard, NextTaskCard, TaskList } from '../src/pages/TasksPage'

describe('Tasks partial failure', () => {
  it('does not describe unavailable upcoming events as zero events', () => {
    const markup = renderToStaticMarkup(createElement(EventsCard, {
      events: [],
      ledgerError: 'invalid ledger',
    }))

    expect(markup).toContain('イベント情報が不完全です')
    expect(markup).toContain('不完全')
    expect(markup).not.toContain('0 件')
    expect(markup).not.toContain('イベントはありません')
  })
})


it('用件と一覧は生成日時と別の期限を表示し、期限超過を保持する', () => {
  const task: TaskView = { task_id: 'synthetic-task', title: '合成の確認用件', kind: 'follow-up', status: 'open', ticker: null, due_date: '2026-09-30', event_label: null, event_date: null, overdue: true }
  const data: TasksView = { generated_at: '2026-10-13T06:49:00Z', tasks_exist: true, open_tasks: [task], next_task: task, upcoming_events: [], earnings_calendar_available: null, ledger_error: null }
  for (const node of [createElement(NextTaskCard, { task }), createElement(TaskList, { data })]) {
    const markup = renderToStaticMarkup(node)
    expect(markup).toMatch(/<time[^>]*dateTime="2026-09-30"[^>]*>期限 /)
    expect(markup).toContain('期限超過')
  }
})


it('does not turn an unavailable earnings schedule into no events or hide reservation expiries', () => {
  const markup = renderToStaticMarkup(createElement(EventsCard, {
    events: [{ event_date: '2026-10-26', kind: 'reservation_expiry', ticker: '1234', label: '1234', days_until: 1 }],
    ledgerError: null, earningsCalendarAvailable: false,
  }))
  expect(markup).toContain('決算予定を確認できません')
  expect(markup).toContain('不完全')
  expect(markup).toContain('予約期限')
  expect(markup).toContain('1234')
  const empty = renderToStaticMarkup(createElement(EventsCard, {
    events: [], ledgerError: null, earningsCalendarAvailable: false,
  }))
  expect(empty).not.toContain('イベントはありません')
  expect(empty).not.toContain('0 件')
})
