import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { TaskView, TasksView } from '../src/api/types'
import { EventsCard, NextTaskCard, OperationCard, TaskList } from '../src/pages/TasksPage'

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


it("運用記録の読込中を記録なしや0件として表示しない", () => {
  const markup = renderToStaticMarkup(createElement(OperationCard, { operations: null, error: null }))
  expect(markup).toContain("運用状況を読み込んでいます")
  expect(markup).toContain('role="status"')
  expect(markup).not.toContain("0 件")
  expect(markup).not.toContain("運用はありません")
})

it("運用記録の取得済み空配列と取得失敗を区別する", () => {
  const empty = renderToStaticMarkup(createElement(OperationCard, { operations: [], error: null }))
  expect(empty).toContain("0 件")
  expect(empty).toContain("運用はありません")
  const failed = renderToStaticMarkup(createElement(OperationCard, { operations: null, error: "failed" }))
  expect(failed).toContain("運用状況を読み込めません")
  expect(failed).not.toContain("読み込んでいます")
  expect(failed).not.toContain("0 件")
})
