import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { ProposalItemCard } from '../src/pages/ProposalPage'
import { assessmentFixture, buyFixture, holdFixture, reviewedDeferFixture, unassessedDeferFixture } from './fixtures/proposals'

function assessment(label = '今回') {
  const result = assessmentFixture(label)
  result.refresh = {
    prior_assessment_ref: { assessment_id: `prior-${label}`, core_sha256: 'c'.repeat(64), review_id: `review-${label}` },
    checked_at: '2026-10-26T14:30:00+09:00',
    changed_sections: ['valuation', 'sources'],
    material_information_check: `${label}：新開示の重要範囲を確認、全文再取得はしていない。`,
    unchanged_assumptions_reason: `${label}：旧根拠を再読し、終価の条件を維持。`,
    horizon_reason: `${label}：3か月経過した残り9か月。旧12か月の自動延長ではない。`,
    distribution_reason: `${label}：旧権利の受取分は除外、今後40円だけの条件。`,
    corporate_action_reason: `${label}：${'一株の権利単位を確認。'.repeat(40)}\n末尾：未公表の変更は保証しない。`,
  }
  return result
}

describe('保存評価の差分確認', () => {
  it('旧参照・確認時刻・全文を固定し、現在の再確認や当初理由と混ぜない', () => {
    const pair = assessment()
    const item = { ...buyFixture, assessment: pair }
    const before = JSON.stringify(item)
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item }))
    expect(html).toContain('評価を更新した根拠')
    for (const key of ['material_information_check', 'unchanged_assumptions_reason', 'horizon_reason', 'distribution_reason', 'corporate_action_reason'] as const) expect(html).toContain(pair.refresh![key])
    for (const value of Object.values(pair.refresh!.prior_assessment_ref)) expect(html).toContain(value)
    expect(html).toContain('dateTime="2026-10-26T14:30:00+09:00"')
    expect(html).toContain('価値算定、資料')
    expect(html).toContain('この評価を更新する際に参照した評価です')
    expect(html).toContain('当初の購入理由や最新評価とは限りません')
    expect(html).toContain('現在の確認・全資料の再取得・判断の有効性を保証しません')
    expect(html).toContain('買付（指値） · buy')
    expect(JSON.stringify(item)).toBe(before)
  })

  it.each(['missing', 'null'] as const)('旧表示を確認済みへ補完しない: %s', kind => {
    const pair = assessment()
    if (kind === 'missing') Reflect.deleteProperty(pair, 'refresh')
    else pair.refresh = null
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item: { ...buyFixture, assessment: pair } }))
    expect(html).toContain('この表示データには差分確認の記録がありません。未実施・不要とは判定しません。')
    expect(html).not.toContain('更新元 assessment_id')
    expect(html).not.toContain('今後40円')
  })

  it('保有の当初評価と今回評価に別の更新元・理由を保持する', () => {
    const item = { ...holdFixture, assessment: assessment('今回'), original_assessment: assessment('当初') }
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item }))
    for (const pair of [item.assessment, item.original_assessment]) {
      expect(html).toContain(pair.refresh!.prior_assessment_ref.assessment_id)
      expect(html).toContain(pair.refresh!.horizon_reason)
    }
    expect(html.match(/評価を更新した根拠/g)).toHaveLength(2)
    expect(html).toContain('保有継続 · hold')
  })

  it('確認文をHTMLとして実行せず、未確定価値と保留を維持する', () => {
    const pair = assessment()
    pair.refresh!.distribution_reason = '<script>bad()</script>\n分配の条件は未確定。'
    pair.refresh!.changed_sections = []
    pair.valuation = { status: 'unresolved', reason: '将来分配をまだ採択できない。' }
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item: { ...reviewedDeferFixture, assessment: pair } }))
    expect(html).toContain('&lt;script&gt;bad()&lt;/script&gt;\n分配の条件は未確定。')
    expect(html).not.toContain('<script>')
    expect(html).toContain('変更区画の記載なし')
    expect(html).toContain('判断保留 · defer')
    expect(html).toContain('価値評価は未確定')
  })

  it('未評価deferへ差分確認や旧評価参照を作らない', () => {
    const html = renderToStaticMarkup(createElement(ProposalItemCard, { item: unassessedDeferFixture }))
    expect(html).not.toContain('評価を更新した根拠')
    expect(html).not.toContain('更新元 assessment_id')
  })
})
