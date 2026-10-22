import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import { AssessmentReturnLink, assessmentReturnHref, savedAssessmentHref } from '../src/components/ReadingNavigation'

describe('保存評価から元の判断へ戻る', () => {
  it('固定評価Aを開き、読了後に同じ提案の対象へ戻る', () => {
    const href = savedAssessmentHref('stock-assessment-A', 'investment-proposal-original')
    expect(href).toBe('/stocks/stock-assessments/stock-assessment-A?proposal=investment-proposal-original')
    const html = renderToStaticMarkup(createElement(MemoryRouter, { initialEntries: [href] }, createElement(AssessmentReturnLink, { ticker: '1234' })))
    expect(html).toContain('href="/stocks/investment-proposals/investment-proposal-original#proposal-item-1234"')
    expect(html).toContain('元の提案・この銘柄の判断に戻る')
    expect(html).not.toContain('stock-assessment-B')
  })

  it('直リンクと空の出発元では一覧へ戻る', () => {
    expect(savedAssessmentHref('stock-assessment-A')).toBe('/stocks/stock-assessments/stock-assessment-A')
    for (const search of ['', '?proposal=']) {
      expect(assessmentReturnHref(search, '1234')).toBe('/stocks')
      const html = renderToStaticMarkup(createElement(MemoryRouter, { initialEntries: [`/stocks/stock-assessments/A${search}`] }, createElement(AssessmentReturnLink, { ticker: '1234' })))
      expect(html).toContain('href="/stocks"')
      expect(html).toContain('提案一覧に戻る')
    }
  })

  it('読込中・失敗でtickerがまだなくても元の提案へ戻れる', () => {
    expect(assessmentReturnHref('?proposal=original')).toBe('/stocks/investment-proposals/original')
    const html = renderToStaticMarkup(createElement(MemoryRouter, { initialEntries: ['/stocks/stock-assessments/A?proposal=original'] }, createElement(AssessmentReturnLink)))
    expect(html).toContain('元の提案に戻る')
    expect(html).not.toContain('#proposal-item-')
  })

  it('出発元をURLとして実行せず提案IDの一部分として扱う', () => {
    expect(assessmentReturnHref('?proposal=https%3A%2F%2Fexample.invalid%2Fa%3Fx%3D1', '1234'))
      .toBe('/stocks/investment-proposals/https%3A%2F%2Fexample.invalid%2Fa%3Fx%3D1#proposal-item-1234')
    expect(savedAssessmentHref('A /', 'P &')).toBe('/stocks/stock-assessments/A%20%2F?proposal=P+%26')
  })
})
