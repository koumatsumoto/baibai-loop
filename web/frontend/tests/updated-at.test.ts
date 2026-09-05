import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { UpdatedAtBadge } from '../src/components/UpdatedAtBadge'

const uiRoot = resolve(import.meta.dirname, '..')

describe('UpdatedAtBadge', () => {
  it('renders the actual timestamp passed by the publication and never invents one', () => {
    const publishedAt = '2026-09-01T18:30:00+09:00'
    const asOf = '2026-08-28'
    const markup = renderToStaticMarkup(createElement(UpdatedAtBadge, { value: publishedAt }))

    expect(markup).toContain('更新 09/01 18:30')
    expect(markup).toContain(`dateTime="${publishedAt}"`)
    expect(markup).not.toContain(asOf)
    expect(renderToStaticMarkup(createElement(UpdatedAtBadge, { value: null }))).toContain('更新 —')
  })

  it.each([
    ['MacroPage.tsx', 'data.latest_context?.published_at ?? null'],
    ['MacroReportPage.tsx', 'data.published_at'],
    ['StocksPage.tsx', 'data.run.generated_at'],
    ['ResearchTriagePage.tsx', 'triage.published_at'],
    ['CapitalAllocationAssessmentPage.tsx', 'data.published_at'],
  ])('%s binds its primary update badge to a real publication or run timestamp', (page, value) => {
    const source = readFileSync(resolve(uiRoot, 'src/pages', page), 'utf8')
    expect(source).toContain(`<UpdatedAtBadge value={${value}}`)
  })
})
