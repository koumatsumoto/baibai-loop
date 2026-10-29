import { createElement, type ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter, Route, Routes } from 'react-router'
import { expect, it } from 'vitest'

import { AppLayout } from '../src/components/AppLayout'
import { PageShell } from '../src/components/PageShell'
import { PageState } from '../src/components/PageState'
import { DashboardPage } from '../src/pages/DashboardPage'
import { TasksPage } from '../src/pages/TasksPage'

function renderInLayout(page: ReactNode): string {
  return renderToStaticMarkup(createElement(MemoryRouter, null,
    createElement(Routes, null,
      createElement(Route, { element: createElement(AppLayout) },
        createElement(Route, { path: '/', element: page }),
      ),
    ),
  ))
}

it.each<[string, ReactNode, string]>([
  ['dashboard loading', createElement(DashboardPage), '資産状況を読み込んでいます'],
  ['tasks loading', createElement(TasksPage), 'Tasks を読み込んでいます'],
  ['read error', createElement(PageState, { message: '読取失敗' }), '読取失敗'],
  ['ready content', createElement(PageShell, { title: '表示内容', children: '用件' }), '用件'],
])('keeps one navigation and one main through %s', (_name, page, content) => {
  const html = renderInLayout(page)
  expect(html.match(/<nav\b/g)).toHaveLength(1)
  expect(html.match(/<main\b/g)).toHaveLength(1)
  expect(html).toContain('aria-label="メインナビゲーション"')
  expect(html).toContain('href="/"')
  expect(html).toContain('href="/tasks"')
  expect(html).toContain(content)
})
