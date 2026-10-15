import { createElement, Fragment } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { RouteErrorBoundary } from '../src/components/RouteErrorBoundary'

describe('route error fallback', () => {
  it('preserves children before an error', () => {
    const child = createElement('p', null, 'content')
    const boundary = new RouteErrorBoundary({ children: child })
    expect(boundary.render()).toBe(child)
  })

  it('offers reload guidance without inventing a failure cause', () => {
    const boundary = new RouteErrorBoundary({ children: null })
    boundary.state = RouteErrorBoundary.getDerivedStateFromError()
    const html = renderToStaticMarkup(createElement(Fragment, null, boundary.render()))
    expect(html).toContain('この画面を表示できませんでした')
    expect(html).toContain('ページを再読み込みしてください')
    expect(html).not.toContain('配信データがこの UI と一致していません')
  })
})
