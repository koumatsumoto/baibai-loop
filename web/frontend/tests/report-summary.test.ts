import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { ReportSummary } from '../src/components/ReportSummary'
import { ProposalItemCard, ProposalReport } from '../src/pages/ProposalPage'
import { buyFixture, newProposalFixture, proposalViewFixture } from './fixtures/proposals'

const reserved = '利益率は回復した。売上も増えた。市場環境は好転した。ただし借換の成立は未確認であり、1,200.50円以下でも成立確認までは買わない。'
const reversed = '利益率は回復した。売上も増えた。市場環境は好転した。しかし返済資金が足りないため、今回は見送る。'

describe('読みやすい提案レポート', () => {
  it.each([reserved, reversed])('後半の重要留保や反対結論を初期表示から切り落とさない', text => {
    const html = renderToStaticMarkup(createElement(ReportSummary, { text }))
    expect(html).toContain(text)
    expect(html).not.toContain('<details')
    expect(html).not.toContain('冒頭3文')
  })

  it('短い保存原文は水増し・再要約せず、文字列をescapeして表示する', () => {
    const text = 'Price is 12.50 yen. 条件付きの判断。<script>alert(1)</script>'
    const html = renderToStaticMarkup(createElement(ReportSummary, { text }))
    expect(html).toContain('Price is 12.50 yen. 条件付きの判断。&lt;script&gt;alert(1)&lt;/script&gt;')
    expect(html).not.toContain('<script>')
  })

  it('保存理由の全文→7モデルチャート→閉じた詳細の順に表示する', () => {
    const item = { ...buyFixture, rationale: reserved }
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item }))
    const summary = html.indexOf('data-report-summary')
    const chart = html.indexOf('7モデルの評価チャート')
    const detail = html.indexOf('data-proposal-details')
    expect(summary).toBeLessThan(chart)
    expect(chart).toBeLessThan(detail)
    expect(html.slice(0, chart)).toContain(reserved)
    expect(html.slice(detail)).not.toContain(reserved)
    expect(html).not.toMatch(/<details[^>]*\bopen(?:=|\s|>)/)
    expect(html).toContain('保存数量')
    expect(html).toContain('1200.0000 円')
  })

  it('レポート冒頭の結論も切らず、比較・見送り・日付は詳細へ保持する', () => {
    const proposal = { ...newProposalFixture, headline: reversed }
    const html = renderToStaticMarkup(createElement(ProposalReport, { data: proposalViewFixture(proposal) }))
    expect(html.indexOf('サマリ')).toBeLessThan(html.indexOf('銘柄別の行動と条件'))
    expect(html.slice(0, html.indexOf('<details'))).toContain(reversed)
    expect(html).toContain(proposal.comparison)
    expect(html).toContain(proposal.forgone)
    expect(html).toContain(`dateTime="${proposal.as_of}"`)
    expect(html).not.toMatch(/<details[^>]*\bopen(?:=|\s|>)/)
  })
})
