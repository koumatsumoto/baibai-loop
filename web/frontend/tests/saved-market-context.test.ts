import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { SavedMarketContextView, SavedProposalView } from '../src/api/types'
import { ProposalReport } from '../src/pages/ProposalPage'
import { newProposalFixture, reportProps, unassessedDeferFixture } from './fixtures/proposals'

const context: SavedMarketContextView = {
  source: 'proposal_fixed_selection', selection_sha256: 'fixed-hash', status: 'retained', reason: null,
  assessment: {
    context_id: 'fixed-market', schema_version: 6, status: 'current', as_of: '2026-10-10', known_at: '2026-10-10T08:00:00Z', published_at: '2026-10-10T09:00:00Z',
    summary: '合成: 金利上昇だけで個社価値を決めない。', changes: [{ summary: '金利変化本文', source_ids: ['rates'] }], counter_evidence: [{ summary: '固定金利の反証末尾', source_ids: ['rates'] }], uncertainties: [{ summary: '借換期は未確認', source_ids: [] }], implications: [{ summary: 'cash維持と借換費用を比較する', source_ids: ['rates'] }],
    sources: [{ input_id: 'rates', reference: 'https://example.invalid/rates', available_at: '2026-10-10T07:00:00Z', status: 'missing' }],
  },
}
const render = (proposal: SavedProposalView, item = proposal.items[0]) => renderToStaticMarkup(createElement(ProposalReport, reportProps(proposal, item)))
const noAction = newProposalFixture.items.find(item => item.action === 'no_action')!

describe('保存された全体市場判断', () => {
  it.each([noAction, unassessedDeferFixture])('情報待ち/見送りを市場支持に変えない', item => {
    const html = render({ ...newProposalFixture, saved_market_context: context, items: [item] }, item)
    for (const text of ['提案開始時に固定した市場判断', '固定金利の反証末尾', '借換期は未確認', 'cash維持と借換費用を比較する', 'rates', 'missing', 'fixed-hash', 'fixed-market', '2026-10-10T08:00:00Z', '2026-10-10T09:00:00Z', '現在の鮮度判定ではありません', '個社価値の採択や売買推奨ではありません']) expect(html).toContain(text)
    expect(html).toContain(item.rationale)
    expect(html).toContain(newProposalFixture.comparison)
    expect(html).toContain(newProposalFixture.forgone)
  })
  it.each(['absent', 'null'] as const)('旧 %s を中立へ補完しない', mode => {
    const proposal = { ...newProposalFixture, saved_market_context: null }
    if (mode === 'absent') delete (proposal as Partial<SavedProposalView>).saved_market_context
    const html = render(proposal)
    expect(html).toContain('この表示データには固定市場判断がありません')
    expect(html).toContain('市場が中立・安全という意味ではありません')
    expect(html).not.toContain('fixed-market')
  })
  it.each(['not_recorded', 'unavailable'] as const)('選定時不在と読取不能を区別する: %s', status => {
    const html = render({ ...newProposalFixture, saved_market_context: { ...context, status, assessment: null, reason: status === 'unavailable' ? 'selection_hash_mismatch' : null } })
    expect(html).toContain(status === 'not_recorded' ? '選定時点では市場判断が保存されていません' : '元の選定と市場判断の対応を読み取れません')
    expect(html).not.toContain('固定金利の反証末尾')
  })
  it('旧時計・旧構造を現在の検証済みへ変換しない', () => {
    const assessment = { ...context.assessment!, known_at: undefined, uncertainties: ['旧形式の留保'], synthesis: { old: '旧の相互作用' } }
    delete assessment.known_at
    const html = render({ ...newProposalFixture, saved_market_context: { ...context, assessment } })
    expect(html).toContain('未記録（他の時刻から推定しません）')
    expect(html).toContain('旧形式の留保')
    expect(html).toContain('旧の相互作用')
    expect(html).not.toContain('2026-10-10T08:00:00Z')
  })
  it('全文末尾とHTMLescapeを保ち引用からリンクを実行しない', () => {
    const marker = '<script>not-executable</script>' + '長い反証'.repeat(180) + '最後の反証'
    const html = render({ ...newProposalFixture, saved_market_context: { ...context, assessment: { ...context.assessment!, counter_evidence: [{ summary: marker, source_ids: ['<img src=x>'] }], sources: [{ reference: 'javascript:bad' }] } } })
    expect(html).toContain('最後の反証')
    expect(html).toContain('&lt;script&gt;not-executable&lt;/script&gt;')
    expect(html).not.toContain('<script>')
    expect(html).not.toContain('href="javascript:')
  })
})
