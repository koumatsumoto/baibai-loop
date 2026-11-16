import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { SavedDeferItemView, SavedHoldingOriginView } from '../src/api/types'
import { ProposalItemCard } from '../src/pages/ProposalPage'
import { assessmentFixture, reviewedDeferFixture, unassessedDeferFixture } from './fixtures/proposals'

const original = assessmentFixture('original-a')
const origin: SavedHoldingOriginView = {
  assessment_ref: { assessment_id: original.assessment_id, core_sha256: original.core_sha256, review_id: original.review_id },
  assessment: original,
  investment_reason: null,
  reason_unavailable: null,
}
const position = {
  quantity: 100, quantity_basis_confirmed: false, cost_yen: '90000',
  inputs_checked_at: '2026-10-27T16:10:00+09:00', ledger_as_of: '2026-10-26T17:00:00+09:00',
}
const render = (item: SavedDeferItemView) => renderToStaticMarkup(createElement(ProposalItemCard, { item }))

describe('判断保留の当初理由は現在の採用仮説ではない', () => {
  it.each([reviewedDeferFixture, unassessedDeferFixture])('今回評価の有無と別に当初理由と反証条件を残す', item => {
    const html = render({ ...item, saved_position: position, saved_holding_origin: origin })
    for (const text of ['当初の投資理由', original.investment_case_explanation, original.invalidation_conditions[0], original.review.strongest_countercase, item.resume_conditions[0], '今回の評価や再確認条件とは別', '保留の解消を意味しません']) expect(html).toContain(text)
    if (item.assessment !== null) expect(html).toContain(item.assessment.investment_case_explanation)
    else expect(html).toContain('未評価：この項目には保存評価への参照がありません')
    expect(html).toContain('判断を保留した不足理由')
    expect(html).not.toContain('保有継続の根拠：')
    expect(html).not.toContain('売却の根拠：')
  })
  it('当初本文が不読でも固定参照を残し今回評価で埋めない', () => {
    const html = render({ ...reviewedDeferFixture, saved_position: position, saved_holding_origin: { ...origin, assessment: null } })
    for (const text of ['対応本文を読み取れません', '今回や最新の評価では補っていません', original.assessment_id, original.core_sha256, original.review_id]) expect(html).toContain(text)
    expect(html).not.toContain(original.investment_case_explanation)
    expect(html).toContain(reviewedDeferFixture.resume_conditions[0])
  })
  it('保存された当初理由の未確認を後発の支持へ補わない', () => {
    const html = render({ ...unassessedDeferFixture, saved_position: position, saved_holding_origin: { assessment_ref: null, assessment: null, investment_reason: null, reason_unavailable: '当初理由の原資料が未確認。' } })
    expect(html).toContain('当初理由の原資料が未確認。')
    expect(html).not.toContain('対応本文を読み取れません')
    expect(html).not.toContain(original.assessment_id)
  })
  it('作者の当初理由を全文保持し HTML を実行しない', () => {
    const text = '<script>bad</script>\n' + '留保。'.repeat(300) + '末尾の反証'
    const html = render({ ...unassessedDeferFixture, saved_position: position, saved_holding_origin: { assessment_ref: null, assessment: null, investment_reason: text, reason_unavailable: null } })
    expect(html).toContain('&lt;script&gt;bad&lt;/script&gt;')
    expect(html).toContain('留保。'.repeat(300) + '末尾の反証')
    expect(html).not.toContain('<script>bad')
  })
  it.each(['absent', 'null'] as const)('旧 %s は当初理由なし/確認済みへ変換しない', mode => {
    const item = { ...reviewedDeferFixture, saved_position: position, saved_holding_origin: null }
    if (mode === 'absent') delete (item as Partial<SavedDeferItemView>).saved_holding_origin
    const html = render(item)
    expect(html).toContain('この表示データには当初理由が未収録です')
    expect(html).toContain(item.resume_conditions[0])
    expect(html).not.toContain(original.assessment_id)
  })
  it('新規探索の defer を保有や当初理由へ補作しない', () => {
    const html = render(unassessedDeferFixture)
    expect(html).not.toContain('当初の投資理由')
    expect(html).toContain(unassessedDeferFixture.resume_conditions[0])
  })
})
