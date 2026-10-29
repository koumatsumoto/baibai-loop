import { Children, createElement, isValidElement, type ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter, Route, Routes } from 'react-router'
import { expect, it, vi } from 'vitest'

import { AppLayout } from '../src/components/AppLayout'
import { PageShell } from '../src/components/PageShell'
import { PageState } from '../src/components/PageState'
import { RouteErrorBoundary } from '../src/components/RouteErrorBoundary'
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

it('keeps navigation outside a failed route boundary', () => {
  const render = RouteErrorBoundary.prototype.render
  const failedBoundary = vi.spyOn(RouteErrorBoundary.prototype, 'render').mockImplementation(
    function (this: RouteErrorBoundary) {
      this.state = RouteErrorBoundary.getDerivedStateFromError()
      return render.call(this)
    },
  )
  try {
    const html = renderInLayout(createElement(PageState, { message: 'page content' }))
    expect(html.match(/<nav\b/g)).toHaveLength(1)
    expect(html.match(/<main\b/g)).toHaveLength(1)
    expect(html).toContain('href="/"')
    expect(html).toContain('href="/tasks"')
    expect(html).toContain('この画面を表示できませんでした')
    expect(html).not.toContain('page content')
  } finally {
    failedBoundary.mockRestore()
  }
})

it.each([
  ['/', '/'],
  ['/tasks', '/tasks'],
  ['/tasks?view=all#top', '/tasks'],
])('keys the page boundary by pathname at %s', (entry, pathname) => {
  const boundaryKeys: (string | null)[] = []
  function InspectLayout() {
    const layout = AppLayout()
    Children.forEach(layout.props.children, (child) => {
      if (isValidElement(child) && child.type === RouteErrorBoundary) boundaryKeys.push(child.key)
    })
    return layout
  }
  renderToStaticMarkup(createElement(MemoryRouter, { initialEntries: [entry] },
    createElement(InspectLayout),
  ))
  expect(boundaryKeys).toEqual([pathname])
})
