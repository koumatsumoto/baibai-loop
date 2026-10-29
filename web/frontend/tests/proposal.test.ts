import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { ProposalItemCard, ProposalReport } from '../src/pages/ProposalPage'
import {
  buyFixture,
  holdFixture,
  holdingProposalFixture,
  newProposalFixture,
  partialBuyFixture,
  proposalViewFixture,
  reviewedDeferFixture,
  sellFixture,
  unassessedDeferFixture,
} from './fixtures/proposals'

function renderProposal() {
  return renderToStaticMarkup(createElement(ProposalReport, { data: proposalViewFixture() }))
}

describe('保存提案レポート', () => {
  it('新規と保有のscopeを分けて、全5actionを保存順で表示する', () => {
    const fresh = renderProposal()
    const holdings = renderToStaticMarkup(createElement(ProposalReport, { data: proposalViewFixture(holdingProposalFixture) }))
    expect(fresh).toContain('新規探索')
    expect(holdings).toContain('保有見直し')
    for (const action of ['買付（指値） · buy', '経済的見送り · no_action', '判断保留 · defer']) expect(fresh).toContain(action)
    for (const action of ['保有継続 · hold', '全量売却 · sell']) expect(holdings).toContain(action)
    let previous = -1
    for (const item of newProposalFixture.items) {
      const current = fresh.indexOf(`>${item.ticker}<`)
      expect(current).toBeGreaterThan(previous)
      previous = current
    }
    expect(fresh).not.toContain('全量売却 · sell')
    expect(holdings).not.toContain('買付（指値） · buy')
    expect(fresh).not.toContain('<table')
    expect(fresh.match(/<main\b/g)).toHaveLength(1)
  })

  it('保存本文・比較・再確認条件・warningsを再要約せず残す', () => {
    const html = renderProposal()
    expect(html).toContain(newProposalFixture.headline)
    expect(html).toContain(newProposalFixture.comparison)
    expect(html).toContain(newProposalFixture.forgone)
    expect(html).toContain(newProposalFixture.warnings[0])
    expect(html).toContain(buyFixture.rationale)
    expect(html).toContain(buyFixture.review_conditions[0])
    expect(html).toContain('合成の具体条件warning。約定を保証しない。')
    expect(html).toContain('whitespace-pre-wrap')
  })

  it('基準日・準備・発行・表示生成・対象取引日を区別し、期限を推定しない', () => {
    const html = renderProposal()
    for (const label of ['提案基準日', '提案本文の準備時刻', '提案の発行時刻', '表示データ生成時刻', '保存時の対象取引日']) expect(html).toContain(label)
    for (const value of ['2026-10-27', '2026-10-27T07:00:01+09:00', '2026-10-27T09:00:02+09:00', '2026-10-29T02:30:03Z', '2026-10-28']) expect(html).toContain(`dateTime="${value}"`)
    expect(html).toContain('保存時点の提案です。現在の注文条件の再確認・採否・発注は別です。')
    expect(html).toContain('対象取引日は実注文の期限ではありません。')
    expect(html).toContain('現在の注文可否は判定していません。')
    expect(html).not.toContain('期限超過')
    expect(html).not.toContain('失効済み')
    expect(html).not.toContain('注文可能')
  })

  it('保存終値・指値・Pmaxは別ラベルでDecimal文字列の精度を保持する', () => {
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item: buyFixture }))
    expect(html).toContain('保存株価（未調整終値）')
    expect(html).toContain('1300.0000000000000001 円')
    expect(html).toContain('1200.0000 円')
    expect(html).toContain('120000.0000 円')
    expect(html).toContain('保存評価の条件付き上限（Pmax）')
    expect(html).toContain('1250.0000000000000001 円')
    expect(html).toContain('保存終値より低い指値で待つ（wait_limit）')
    expect(html).not.toContain('成行')
  })

  it.each(['place_limit', 'wait_limit'] as const)('termsなしbuyも作者指定上限と不足理由を残す: %s', timing => {
    const proposedLimit = timing === 'wait_limit' ? '1200.1000000000000001' : partialBuyFixture.proposed_limit_yen
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item: { ...partialBuyFixture, timing, proposed_limit_yen: proposedLimit } }))
    expect(html).toContain('具体条件未確定')
    expect(html).toContain('作者指定の上限（具体条件未確定）')
    expect(html).toContain(`${proposedLimit} 円`)
    expect(html).toContain('保存された銘柄分類を確認できない。')
    expect(html).not.toContain('保存数量')
    expect(html).not.toContain('保存想定金額')
    expect(html).not.toContain('保存時の対象取引日')
    expect(html).not.toContain('0 株')
  })

  it('保有の今回Bと当初Aを分離し、当初理由を別latestへ付け替えない', () => {
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item: holdFixture }))
    expect(html).toContain('今回の保存評価・独立検証の抜粋')
    expect(html).toContain('当初の投資理由')
    expect(html).toContain('assessment-original-a')
    expect(html).toContain('assessment-current-b')
    expect(html).toContain('original-aの投資理由')
    expect(html).toContain('current-bの投資理由')
    expect(html).toContain('今回の再評価理由')
    expect(html).toContain(holdFixture.case_reassessment_reason)
    expect(html).toContain('投資仮説の維持')
    expect(html).toContain(holdFixture.return_calculation_unavailable_reason)
    expect(html).not.toContain('latest-c')
    expect(html).toContain('<details')
    expect(html).toContain('<summary')
  })

  it('当初理由の保存本文と取得不能を区別し、売却terms不足をholdへ変えない', () => {
    const sell = renderToStaticMarkup(createElement(ProposalItemCard, { item: sellFixture }))
    expect(sell).toContain(sellFixture.original_investment_reason)
    expect(sell).toContain(sellFixture.case_reassessment_reason)
    expect(sell).toContain('全量売却 · sell')
    expect(sell).toContain('具体条件未確定')
    expect(sell).toContain(sellFixture.terms_unavailable_reasons[0].message)
    expect(sell).not.toContain('保有継続 · hold')
    const missing = renderToStaticMarkup(createElement(ProposalItemCard, { item: {
      ...holdFixture, original_assessment: null, original_reason_unavailable_reason: '当初理由の記録がない。',
    } }))
    expect(missing).toContain('当初理由を確認できない理由')
    expect(missing).toContain('当初理由の記録がない。')
    expect(missing).not.toContain('assessment-original-a')
  })

  it('部分確認のReviewとuncertain評価を全面確認や未評価に潰さない', () => {
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item: reviewedDeferFixture }))
    expect(html).toContain('不確実（uncertain）')
    expect(html).toContain('部分確認（partially_verified）')
    expect(html).toContain('独立検証時刻')
    expect(html).toContain('Reviewの存在は、すべての事実の確認や現在の注文資格を示しません。')
    expect(html).toContain(reviewedDeferFixture.assessment?.review.review_scope)
    expect(html).toContain(reviewedDeferFixture.assessment?.review.strongest_countercase)
    expect(html).toContain(reviewedDeferFixture.assessment?.review.uncertainty_acceptance_reason)
    expect(html).toContain(reviewedDeferFixture.resume_conditions[0])
    expect(html).not.toContain('未評価：')
    expect(html).not.toContain('承認済み')
  })

  it('未評価deferの未完了理由・参照と再開条件を残す', () => {
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item: unassessedDeferFixture }))
    expect(html).toContain('未評価：この項目には保存評価への参照がありません。')
    expect(html).toContain(unassessedDeferFixture.unfinished?.reason)
    expect(html).toContain('synthetic:research-missing-document')
    expect(html).toContain(unassessedDeferFixture.resume_conditions[0])
    expect(html).not.toContain('今回の保存評価・独立検証の抜粋')
  })

  it('proposalとexact評価pairの識別子とコピー導線を提供する', () => {
    const html = renderProposal()
    expect(html).toContain(newProposalFixture.proposal_id)
    for (const label of ['proposal_id', 'assessment_id', 'core_sha256', 'review_id']) expect(html).toContain(`aria-label="${label}をコピー"`)
    expect(html).toContain('select-text break-all')
    expect(html).toContain('aria-live="polite"')
    expect(html).not.toContain('href=')
  })

  it('流動性と集中の保存値・不足・coverageを小さな補足で保持する', () => {
    const html = renderProposal()
    expect(html).toContain('保存された流動性の補足')
    expect(html).toContain('9876543.0000000001 円')
    expect(html).toContain('売買代金の保存モデルが未取得。')
    expect(html).toContain('保存された資本集中の補足')
    expect(html).toContain('1000000.0000000001 円')
    expect(html).toContain('12.0000000000000001 %')
    expect(html).toContain('40.0000000000000001 %')
    expect(html).toContain('共通要因が未分類の銘柄')
    expect(html).toContain('9999')
    expect(html).toContain('共通要因の比率は既知部分のみです。')
    const absent = renderToStaticMarkup(createElement(ProposalReport, { data: proposalViewFixture(holdingProposalFixture) }))
    expect(absent).toContain('銘柄別集中：保存値なし')
    expect(absent).not.toContain('0 円')
    expect(absent).not.toContain('0 %')
  })

  it('正常empty、読取不能、loadingを区別して空提案を捏造しない', () => {
    const base = { generated_at: '2026-10-29T02:30:03Z', proposal: null }
    const empty = renderToStaticMarkup(createElement(ProposalReport, { data: { ...base, status: 'empty', error: null } }))
    const error = renderToStaticMarkup(createElement(ProposalReport, { data: { ...base, status: 'unavailable', error: '保存済み提案を読み取れませんでした。' } }))
    const loading = renderToStaticMarkup(createElement(ProposalReport, { data: null }))
    expect(empty).toContain('保存済み提案はありません')
    expect(empty).toContain('表示データ生成')
    expect(error).toContain('売買提案の読取不能')
    expect(error).not.toContain('保存済み提案はありません')
    expect(loading).toContain('売買提案を読み込んでいます')
    for (const html of [empty, error, loading]) expect(html).not.toContain('0 円')
  })

  it('保存本文をHTMLとして解釈せず、改行を保持する', () => {
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item: { ...buyFixture, rationale: '<script>alert(1)</script>\n保存文章の二行目' } }))
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;\n保存文章の二行目')
    expect(html).not.toContain('<script>')
  })
})
