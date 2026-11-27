import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'

import { AppShell } from '../src/components/AppShell'
import { ACTIONS_URL, NAV_TABS } from '../src/lib/nav'

function activeLabels(pathname: string): string[] {
  return NAV_TABS.filter((tab) => tab.match(pathname)).map((tab) => tab.label)
}

describe('NAV_TABS active matching', () => {
  it.each<[string, string[]]>([
    ['/', ['資産状況']],
    ['/tasks', []],
    ['/proposal', ['日本株']],
    ['/proposal/unknown', []],
    ['/us-proposal', ['米国株']],
    ['/us-proposal/unknown', []],
    ['/nowhere', []],
    ['/tasks/unknown', []],
  ])('selects only the current page for %s', (pathname, labels) => {
    expect(activeLabels(pathname)).toEqual(labels)
  })
})

describe('shared primary navigation', () => {
  it.each([
    ['/', '/'],
    ['/proposal?proposal=saved-id&ticker=4755#top', '/proposal'],
    ['/us-proposal', '/us-proposal'],
    ['/nowhere', null],
  ])('marks only the current page at %s', (entry, activeHref) => {
    const html = renderToStaticMarkup(createElement(MemoryRouter, { initialEntries: [entry] }, createElement(AppShell)))
    const nav = html.match(/<nav\b[^>]*>[\s\S]*?<\/nav>/)?.[0] ?? ''
    const links = [...nav.matchAll(/<a\b([^>]*)>([\s\S]*?)<\/a>/g)]

    expect(links).toHaveLength(3)
    expect(links.map((link) => link[1].match(/href="([^"]*)"/)?.[1])).toEqual(['/', '/proposal', '/us-proposal'])
    expect(links.map((link) => link[2].replace(/<[^>]*>/g, ''))).toEqual(['資産状況', '日本株', '米国株'])
    expect(links.map((link) => link[1].match(/aria-label="([^"]*)"/)?.[1]))
      .toEqual([undefined, '日本株の売買提案', '米国株の判断'])
    expect(nav).toContain('grid-cols-3')
    for (const [, attributes, contents] of links) {
      expect(attributes).toContain('min-h-10')
      expect(attributes).toContain('justify-center')
      expect(attributes).toContain('whitespace-nowrap')
      expect(attributes).toContain('focus-visible:outline-2')
      expect(attributes.includes('aria-current="page"')).toBe(attributes.includes(`href="${activeHref}"`))
      expect(contents).toContain('aria-hidden="true"')
    }
  })
})

describe('ACTIONS_URL', () => {
  it('points at the daily batch workflow over https', () => {
    expect(ACTIONS_URL.startsWith('https://github.com/')).toBe(true)
    expect(ACTIONS_URL).toContain('cloud-daily-batch-current.yml')
  })
})
