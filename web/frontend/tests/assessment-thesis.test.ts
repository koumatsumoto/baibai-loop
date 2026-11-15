import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { ProposalItemCard } from '../src/pages/ProposalPage'
import { assessmentFixture, buyFixture, holdFixture, reviewedDeferFixture, unassessedDeferFixture } from './fixtures/proposals'

function assessment(label = '今回') {
  const result = assessmentFixture(label)
  result.strongest_countercase = `${label}作者の反証：${'借換は未確約。'.repeat(40)}\n末尾：追加資本の影響を残す。`
  result.realization = {
    mechanism: `${label}：受注から検収・現金回収へ届く経路。`,
    timing: 'uncertain',
    timing_reason: `${label}：入金日は未確定。比較期間12か月と同じではない。`,
    delay_risk: `${label}：回収遅延で資金需要が増える可能性。`,
  }
  result.valuation = {
    status: 'resolved', price_yen: '1000', price_as_of: '2026-10-26', price_observed_at: null,
    price_basis: 'last_close_unadjusted', horizon_months: 12, required_annual_return_pct: '6',
    base: { terminal_value_per_share_yen: '1100', cash_distribution_per_share_yen: '0', annualized_return_pct: '10', calculation: '合成の中心条件' },
    downside: { terminal_value_per_share_yen: '700', cash_distribution_per_share_yen: '0', annualized_return_pct: '-30', calculation: '合成の下方条件' },
  }
  return result
}

describe('保存評価の作者反証と価値実現条件', () => {
  it.each(['known', 'uncertain'] as const)('別々の反対論・全文・実現時期の判断を保存値で示す: %s', timing => {
    const pair = assessment()
    pair.realization!.timing = timing
    const item = { ...buyFixture, assessment: pair }
    const before = JSON.stringify(item)
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item }))
    expect(html).toContain(pair.strongest_countercase)
    expect(html).toContain(pair.review.strongest_countercase)
    expect(html).toContain('評価作者の最も強い反対論')
    expect(html).toContain('独立検証の最も強い反対論')
    for (const key of ['mechanism', 'timing_reason', 'delay_risk'] as const) expect(html).toContain(pair.realization![key])
    expect(html).toContain(timing === 'known' ? '作者が時期を把握と記録' : '未確定')
    expect(html).toContain('評価の比較期間とは別で、実現や現在の裏付けを保証しません')
    expect(html).toContain('評価時点からの期間')
    expect(html).toContain('12 か月')
    expect(html).toContain('買付（指値） · buy')
    expect(JSON.stringify(item)).toBe(before)
  })

  it.each(['missing', 'null'] as const)('旧表示JSONの未収録を反証/遅延リスクなしへ補完しない: %s', kind => {
    const pair = assessment()
    if (kind === 'missing') {
      Reflect.deleteProperty(pair, 'strongest_countercase')
      Reflect.deleteProperty(pair, 'realization')
    } else {
      pair.strongest_countercase = null
      pair.realization = null
    }
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item: { ...buyFixture, assessment: pair } }))
    expect(html).toContain('この表示データには評価作者の反対論が含まれていません。反証がないことを示しません。')
    expect(html).toContain('この表示データには価値実現の条件が含まれていません。遅延リスクがないことを示しません。')
    expect(html).toContain(pair.review.strongest_countercase)
    expect(html).toContain('買付（指値） · buy')
  })

  it('当初と今回を分離し、いずれもReviewerの反論と混ぜない', () => {
    const item = structuredClone(holdFixture)
    item.assessment = assessment('今回')
    item.original_assessment = assessment('当初')
    item.original_assessment.as_of = '2026-07-01'
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item }))
    for (const pair of [item.assessment, item.original_assessment]) {
      expect(html).toContain(pair.strongest_countercase)
      expect(html).toContain(pair.realization!.mechanism)
      expect(html).toContain(pair.review.strongest_countercase)
    }
    expect(html.match(/評価作者の最も強い反対論/g)).toHaveLength(2)
    expect(html).toContain('dateTime="2026-07-01"')
    expect(html).toContain('保有継続 · hold')
  })

  it('文章をHTMLにせず、判断未完や価値未確定を維持する', () => {
    const pair = assessment()
    pair.stance = 'uncertain'
    pair.investment_case_status = 'uncertain'
    pair.valuation = { status: 'unresolved', reason: '追加資本の条件が未確定。' }
    pair.strongest_countercase = '<script>bad()</script>\n作者の留保'
    pair.realization!.mechanism = '<img src=x>現金回収を保証しない'
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item: { ...reviewedDeferFixture, assessment: pair } }))
    expect(html).toContain('&lt;script&gt;bad()&lt;/script&gt;\n作者の留保')
    expect(html).toContain('&lt;img src=x&gt;現金回収を保証しない')
    expect(html).not.toContain('<script>')
    expect(html).not.toContain('<img src=x>')
    expect(html).toContain('判断保留 · defer')
    expect(html).toContain('価値評価は未確定')
    expect(html).not.toContain('12 か月')
  })

  it('未評価deferに作者仮説や反証を創作しない', () => {
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item: unassessedDeferFixture }))
    expect(html).toContain('判断保留 · defer')
    expect(html).not.toContain('評価作者の最も強い反対論')
    expect(html).not.toContain('価値実現の機序と時間')
  })
})
