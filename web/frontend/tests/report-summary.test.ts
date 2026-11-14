import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { ReportSummary } from '../src/components/ReportSummary'
import { reportExcerpt } from '../src/lib/report-excerpt'
import { ProposalItemCard, ProposalReport } from '../src/pages/ProposalPage'
import { buyFixture, newProposalFixture, proposalViewFixture } from './fixtures/proposals'

const text = '利益率は回復した。借換費用には注意が必要。1,200.50円以下を条件とする。第四文は原典で再確認する。'

describe('読みやすい提案レポート', () => {
  it('小数点・日本語を壊さず最大3文を原文のまま抜粋する', () => {
    expect(reportExcerpt(text)).toBe('利益率は回復した。借換費用には注意が必要。1,200.50円以下を条件とする。')
    expect(reportExcerpt('短い保存判断。条件付き。')).toBe('短い保存判断。条件付き。')
    expect(reportExcerpt('句点のない原文を途中で切らない')).toBe('句点のない原文を途中で切らない')
    expect(reportExcerpt('Price is 12.50 yen. Profit improved. Risk remains. Full evidence follows.')).toBe('Price is 12.50 yen. Profit improved. Risk remains.')
  })

  it('短い原文を水増しせず、長い本文は抜粋だと明記する', () => {
    const long = renderToStaticMarkup(createElement(ReportSummary, { text }))
    const short = renderToStaticMarkup(createElement(ReportSummary, { text: '短い保存判断。' }))
    expect(long).toContain('保存本文の冒頭3文')
    expect(long).not.toContain('第四文')
    expect(short).not.toContain('冒頭3文')
  })

  it('要点→7モデルチャート→閉じた詳細の順に表示し、全文は失わない', () => {
    const item = { ...buyFixture, rationale: text }
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item }))
    const summary = html.indexOf('data-report-summary')
    const chart = html.indexOf('7モデルの評価チャート')
    const detail = html.indexOf('data-proposal-details')
    expect(summary).toBeLessThan(chart)
    expect(chart).toBeLessThan(detail)
    expect(html.slice(0, chart)).not.toContain('第四文')
    expect(html.slice(detail)).toContain(text)
    expect(html).not.toMatch(/<details[^>]*\bopen(?:=|\s|>)/)
    expect(html).toContain('保存数量')
    expect(html).toContain('1200.0000 円')
  })

  it('レポート冒頭も簡潔にし、結論・比較・見送りの全文と日付は詳細へ保持する', () => {
    const proposal = { ...newProposalFixture, headline: text }
    const html = renderToStaticMarkup(createElement(ProposalReport, { data: proposalViewFixture(proposal) }))
    expect(html.indexOf('サマリ')).toBeLessThan(html.indexOf('銘柄別の行動と条件'))
    expect(html).toContain(text)
    expect(html).toContain(proposal.comparison)
    expect(html).toContain(proposal.forgone)
    expect(html).toContain(`dateTime="${proposal.as_of}"`)
    expect(html).not.toMatch(/<details[^>]*\bopen(?:=|\s|>)/)
  })
})
