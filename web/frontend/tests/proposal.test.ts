import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'

import type { ProposalHistoryView } from '../src/api/types'
import { ProposalHistory } from '../src/pages/ProposalHistoryPage'
import { ProposalItemCard, ProposalReport } from '../src/pages/ProposalPage'
import {
  buyFixture,
  holdFixture,
  holdingProposalFixture,
  newProposalFixture,
  partialBuyFixture,
  reportProps,
  reviewedDeferFixture,
  sellFixture,
  unassessedDeferFixture,
} from './fixtures/proposals'

function renderProposal(proposal = newProposalFixture, item = proposal.items[0]) {
  return renderToStaticMarkup(createElement(ProposalReport, reportProps(proposal, item)))
}

describe('保存提案レポート', () => {
  it('選んだ銘柄のscopeとactionだけを示し、別の銘柄の根拠を混ぜない', () => {
    const labels = { buy: '買付（指値） · buy', no_action: '経済的見送り · no_action', defer: '判断保留 · defer', hold: '保有継続 · hold', sell: '全量売却 · sell' }
    for (const [proposal, scope] of [[newProposalFixture, '新規探索'], [holdingProposalFixture, '保有見直し']] as const) {
      for (const item of proposal.items) {
        const html = renderProposal(proposal, item)
        expect(html).toContain(scope)
        expect(html).toContain(labels[item.action])
        expect(html).toContain(item.rationale)
        for (const other of proposal.items.filter(other => other.action !== item.action)) {
          expect(html).not.toContain(other.rationale)
        }
        expect(html.includes('売買判断の7つの観点')).toBe(item.assessment !== null)
        expect(html.match(/<main\b/g)).toHaveLength(1)
      }
    }
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
    expect(html).toContain('保存時点のレポートです。現在の注文条件の再確認・採否・発注は別です。')
    expect(html).toContain('対象取引日は実注文の期限ではありません。')
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
    expect(html).toContain('100000.000000001 円')
    expect(html).toContain('19 / 20 本の価格bar')
    expect(html).toContain('2026-09-25 ～ 2026-10-23')
    expect(html).toContain('ADVの価格窓終端日')
    expect(html).toContain('dateTime="2026-10-23"')
    expect(html).toContain('dateTime="2026-10-26"')
    expect(html).toContain('価格窓の終端であり、売買代金の最終有効日や鮮度の保証ではありません。')
    expect(renderProposal(newProposalFixture, reviewedDeferFixture)).toContain('売買代金の保存モデルが未取得。')
    expect(html).toContain('保存された資本集中の補足')
    expect(html).toContain('1000000.0000000001 円')
    expect(html).toContain('12.0000000000000001 %')
    expect(html).toContain('40.0000000000000001 %')
    expect(html).toContain('共通要因が未分類の銘柄')
    expect(html).toContain('9999')
    expect(html).toContain('共通要因の比率は既知部分のみです。')
    const absent = renderProposal(holdingProposalFixture)
    expect(absent).toContain('銘柄別集中：保存値なし')
    expect(absent).not.toContain('0 円')
    expect(absent).not.toContain('0 %')
  })

  it('日付のない旧表示データは分析基準日から価格窓終端を補完しない', () => {
    const legacy = structuredClone(buyFixture)
    Reflect.deleteProperty(legacy.liquidity!, 'price_window_end')
    Reflect.deleteProperty(legacy.liquidity!, 'turnover_window')
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item: legacy }))
    expect(html).toMatch(/ADVの価格窓終端日<\/dt><dd[^>]*>保存値なし<\/dd>/)
    expect(html).not.toContain('dateTime="2026-10-23"')
    expect(html).toContain('dateTime="2026-10-26"')
    expect(html).toContain('売買代金窓の本数・中央値は保存値なし。')
    expect(html).not.toContain('100000.000000001 円')
  })

  it('売買代金の0観測と中央値未算出を平均で埋めない', () => {
    const item = structuredClone(buyFixture)
    item.liquidity!.turnover_window!.valid_turnover_count = 0
    item.liquidity!.turnover_window!.median_yen = null
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item }))
    expect(html).toContain('0 / 20 本の価格bar')
    expect(html).toContain('算出に必要な有効観測数なし')
    expect(html).toContain('9876543.0000000001 円')
    expect(html).not.toContain('中央値</dt><dd>0 円')
  })

  it('保存本文をHTMLとして解釈せず、改行を保持する', () => {
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item: { ...buyFixture, rationale: '<script>alert(1)</script>\n保存文章の二行目' } }))
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;\n保存文章の二行目')
    expect(html).not.toContain('<script>')
  })
})


it('システム提案と訂正、人間の非購入報告を分け、未知の注文結果を補完しない', () => {
  const proposal = {
    ...newProposalFixture,
    corrections: [{
      correction_id: 'synthetic-correction',
      erroneous_ref: { proposal_id: 'synthetic-wrong', draft_sha256: '0'.repeat(64) },
      replacement_ref: { proposal_id: newProposalFixture.proposal_id, draft_sha256: '1'.repeat(64) },
      reason: '合成の訂正理由。',
      recorded_at: '2026-10-29T02:00:00Z',
    }],
    human_reports: [{
      report_id: 'synthetic-report',
      proposal_ref: { proposal_id: newProposalFixture.proposal_id, draft_sha256: '1'.repeat(64) },
      outcome: 'not_purchased' as const,
      statement: '今回は購入しなかった。',
      reported_at: '2026-10-29T01:00:00Z',
      subject_at: null,
      reason_quote: '今回は待つ。',
      context_summary: '合成の担当者要約。',
      recorded_at: '2026-10-29T02:00:00Z',
    }],
  }
  const html = renderProposal(proposal)
  for (const text of ['買付（指値） · buy', '合成の訂正理由。', '購入しなかった', '本人の理由（引用）', '会話文脈の要約（担当者）', '今回は待つ。', '合成の担当者要約。', '現在の再推奨や人間の採用ではありません。', '未発注・取消・未約定・永久却下は、この報告からは確認できません。']) expect(html).toContain(text)
  expect(html).toContain(newProposalFixture.headline)
  expect(html).toContain(`dateTime="${newProposalFixture.published_at}"`)
})

it('銘柄別レポートでも、非購入報告は銘柄別の行動ではなく提案単位で表示する', () => {
  const statement = `${buyFixture.ticker} は買いませんでした。ほかの銘柄については報告していません。`
  const proposal = {
    ...newProposalFixture,
    human_reports: [{
      report_id: 'synthetic-scope-report',
      proposal_ref: { proposal_id: newProposalFixture.proposal_id, draft_sha256: '1'.repeat(64) },
      outcome: 'not_purchased' as const,
      statement,
      reported_at: '2026-10-29T01:00:00Z',
      subject_at: null,
      reason_quote: null,
      context_summary: null,
      recorded_at: '2026-10-29T02:00:00Z',
    }],
  }
  const data: ProposalHistoryView = {
    generated_at: '2026-10-29T02:30:03Z', status: 'ready', error: null,
    proposals: [proposal], unavailable_proposal_ids: [],
  }
  const noAction = proposal.items.find(item => item.action === 'no_action')!
  for (const [item, other] of [[buyFixture, noAction], [noAction, buyFixture]] as const) {
    const html = renderToStaticMarkup(createElement(MemoryRouter, null, createElement(ProposalHistory, {
      data, proposalId: proposal.proposal_id, ticker: item.ticker,
    })))
    const tickerSection = html.indexOf('<section aria-labelledby="proposal-items-heading"')
    expect(tickerSection).toBeGreaterThan(0)
    const proposalScope = html.slice(0, tickerSection)
    expect(proposalScope).toContain('提案単位の人間の報告（取引事実とは別）')
    expect(proposalScope).toContain('報告はこの提案にひもづきます。どの銘柄について述べたかは、本人の文面で確認してください。')
    expect(proposalScope).toContain(statement)
    expect(html.slice(tickerSection)).not.toContain(statement)
    expect(html).toContain(item.rationale)
    expect(html).not.toContain(other.rationale)
  }
})
