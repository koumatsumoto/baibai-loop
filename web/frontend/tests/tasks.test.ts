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
  const data: TasksView = { generated_at: '2026-10-13T06:49:00Z', tasks_exist: true, open_tasks: [task], next_task: task, upcoming_events: [], ledger_error: null }
  for (const node of [createElement(NextTaskCard, { task }), createElement(TaskList, { data })]) {
    const markup = renderToStaticMarkup(node)
    expect(markup).toMatch(/<time[^>]*dateTime="2026-09-30"[^>]*>期限 /)
    expect(markup).toContain('期限超過')
  }
})
