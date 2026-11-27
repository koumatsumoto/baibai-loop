import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'

import { AppShell } from '../src/components/AppShell'
import { ACTIONS_URL } from '../src/lib/nav'

describe('shared primary navigation', () => {
  it.each([
    ['/', '/', '/proposal'],
    ['/proposal', '/proposal', '/proposal'],
    ['/proposal?proposal=saved-id&ticker=4755#top', '/proposal', '/proposal'],
    ['/proposal/', '/proposal', '/proposal'],
    ['/us-proposal', '/us-proposal', '/us-proposal'],
    ['/us-proposal/?ignored=value', '/us-proposal', '/us-proposal'],
    ['/proposal/unknown', null, '/proposal'],
    ['/us-proposal/unknown', null, '/proposal'],
    ['/nowhere', null, '/proposal'],
    ['/tasks', null, '/proposal'],
  ])('shows one proposal entry and marks the active page at %s', (entry, activeHref, proposalHref) => {
    const html = renderToStaticMarkup(createElement(MemoryRouter, { initialEntries: [entry] }, createElement(AppShell)))
    const nav = html.match(/<nav\b[^>]*>[\s\S]*?<\/nav>/)?.[0] ?? ''
    const links = [...nav.matchAll(/<a\b([^>]*)>([\s\S]*?)<\/a>/g)]

    expect(links.map((link) => link[1].match(/href="([^"]*)"/)?.[1])).toEqual(['/', proposalHref])
    expect(links.map((link) => link[2].replace(/<[^>]*>/g, ''))).toEqual(['資産状況', '売買提案'])
    expect(nav).toContain('grid-cols-2')
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
