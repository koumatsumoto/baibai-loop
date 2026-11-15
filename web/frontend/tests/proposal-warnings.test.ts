import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { ProposalItemCard, ProposalReport } from '../src/pages/ProposalPage'
import { buyFixture, holdingProposalFixture, newProposalFixture, proposalViewFixture, sellFixture, termsFixture } from './fixtures/proposals'

function renderTerms(warnings: string[], sell = false) {
  const item = sell
    ? { ...sellFixture, terms: { ...termsFixture, maximum_entry_price_yen: null, warnings }, terms_unavailable_reasons: [] }
    : { ...buyFixture, terms: { ...termsFixture, warnings } }
  return renderToStaticMarkup(createElement(ProposalItemCard, { item }))
}

describe('保存注意の意味と元記録', () => {
  it('保有判断と台帳予約を区別し、holdを取消命令や売買阻止に変えない', () => {
    const warnings = ['hold_active_reservation_requires_confirmation:1234']
    const before = JSON.stringify(holdingProposalFixture)
    const html = renderToStaticMarkup(createElement(ProposalReport, {
      data: proposalViewFixture({ ...holdingProposalFixture, warnings }),
    }))
    expect(html).toContain('保有継続 · hold')
    expect(html).toContain('保存入力の台帳に、保有継続を提案した銘柄の未完了予約が残っています。')
    expect(html).toContain('保有判断は既存注文の取消・失効確認ではありません。')
    expect(html).toContain('現在の注文状態は別に確認してください。')
    expect(html).toContain('対象：1234')
    expect(html).toContain(warnings[0])
    expect(JSON.stringify(holdingProposalFixture)).toBe(before)
  })

  it('予約注意のない旧表示へ現在の予約や取消済みを補わない', () => {
    const html = renderToStaticMarkup(createElement(ProposalReport, {
      data: proposalViewFixture({ ...holdingProposalFixture, warnings: [] }),
    }))
    expect(html).not.toContain('hold_active_reservation_requires_confirmation')
    expect(html).not.toContain('未完了予約が残っています。')
    expect(html).not.toContain('取消済み')
  })

  it('予約注意の対象もescapeし、空の対象は未知codeのまま残す', () => {
    const html = renderTerms(['hold_active_reservation_requires_confirmation:<script>x</script>'])
    expect(html).toContain('対象：&lt;script&gt;x&lt;/script&gt;')
    expect(html).not.toContain('<script>')
    const malformed = renderTerms(['hold_active_reservation_requires_confirmation:'])
    expect(malformed).toContain('hold_active_reservation_requires_confirmation:')
    expect(malformed).not.toContain('未完了予約が残っています。')
  })

  it.each([false, true])('ADV超過と未評価を日本語で区別し、buy/sellを変更しない: %s', sell => {
    const exceeded = renderTerms(['adv_participation_exceeds_warning'], sell)
    const unknown = renderTerms(['adv_participation_unassessed'], sell)
    expect(exceeded).toContain('売買代金に対する提案規模が、保存時の注意水準を超えています。')
    expect(unknown).toContain('売買代金に対する提案規模は未評価です。')
    expect(unknown).toContain('注意がない・小さいとは判定していません。')
    for (const html of [exceeded, unknown]) {
      expect(html).toContain(sell ? '全量売却 · sell' : '買付（指値） · buy')
      expect(html).toContain('現在の注文可否や、自動的な売買・数量変更の指示ではありません。')
      expect(html).toContain(termsFixture.limit_price_yen)
      expect(html).toContain(termsFixture.notional_yen)
    }
    expect(exceeded).toContain('<code')
    expect(exceeded).toContain('adv_participation_exceeds_warning')
    expect(unknown).toContain('adv_participation_unassessed')
    expect(unknown).not.toContain('保存時の注意水準を超えています。')
  })

  it('予算guideとcash余力の注意を固定上限や購入quotaに変えない', () => {
    const html = renderTerms(['budget_guide_under', 'budget_guide_exceeded', 'dry_powder_below_floor'])
    expect(html).toContain('買付金額が目安を下回ります。目安まで増やす指示ではありません。')
    expect(html).toContain('買付金額が目安を上回ります。資金不足の判定とは別です。')
    expect(html).toContain('この買付後の未拘束現金が、保存時の注意水準を下回ります。')
    for (const code of ['budget_guide_under', 'budget_guide_exceeded', 'dry_powder_below_floor']) expect(html).toContain(code)
    expect(html).not.toContain('20%')
    expect(html).not.toContain('5%')
  })

  it.each([
    ['prospective_ticker_concentration_exceeds_warning:9127', '銘柄別の集中が、保存時の注意水準を超えています。', '9127'],
    ['prospective_sector_concentration_exceeds_warning:海運業', '業種別の集中が、保存時の注意水準を超えています。', '海運業'],
    ['prospective_common_factor_concentration_exceeds_warning:需要循環:共通', '共通要因への集中が、保存時の注意水準を超えています。', '需要循環:共通'],
  ])('対象付きの集中注意を元codeごと保持する: %s', (code, explanation, target) => {
    const html = renderTerms([code])
    expect(html).toContain(explanation)
    expect(html).toContain(`対象：${target}`)
    expect(html).toContain(code)
  })

  it('提案全体の未評価とcoverage不足を、確認済みの低リスクへ変換しない', () => {
    const warnings = ['portfolio_valuation_unknown:9127', 'concentration_and_dry_powder_unassessed', 'portfolio_exposure_common_factor_coverage_incomplete']
    const html = renderToStaticMarkup(createElement(ProposalReport, {
      data: proposalViewFixture({ ...newProposalFixture, warnings }),
    }))
    expect(html).toContain('保存時に時価を確認できない保有銘柄があります。')
    expect(html).toContain('集中度と現金余力の診断は未評価です。')
    expect(html).toContain('共通要因が未分類の銘柄があります。見えている集中度だけで全体を評価できません。')
    expect(html).toContain('対象：9127')
    for (const code of warnings) expect(html).toContain(code)
  })

  it('未知code・旧作者文・順序を改変せず、入力も変更しない', () => {
    const warnings = ['new_warning_v9:未知', '作者の注意。\n末尾も残す。', 'prospective_sector_concentration_exceeds_warning:', 'constructor', 'adv_participation_unassessed']
    const before = JSON.stringify(warnings)
    const html = renderTerms(warnings)
    let previous = -1
    for (const warning of warnings) {
      const index = html.indexOf(warning)
      expect(index).toBeGreaterThan(previous)
      previous = index
    }
    expect(html).toContain('whitespace-pre-wrap')
    expect(html).toContain('売買代金に対する提案規模は未評価です。')
    expect(JSON.stringify(warnings)).toBe(before)
  })

  it('対象と未知文をHTMLとして実行せず、空の注意を安全確認に変えない', () => {
    const code = 'prospective_common_factor_concentration_exceeds_warning:<img src=x onerror=alert(1)>'
    const html = renderTerms([code, '<script>alert(1)</script>'])
    expect(html).toContain('共通要因への集中が、保存時の注意水準を超えています。')
    expect(html).toContain('&lt;img src=x onerror=alert(1)&gt;')
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;')
    expect(html).not.toContain('<img src=x')
    expect(html).not.toContain('<script>')
    expect(renderTerms([])).not.toContain('具体条件の保存時の注意事項')
  })
})
