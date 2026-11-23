import type { SavedProposalView } from '../api/types'
import { jstDateTimeParts } from './format'

export const ACTION_LABEL: Record<SavedProposalView['items'][number]['action'], string> = {
  buy: '保存時の買付（指値）',
  hold: '保有継続',
  sell: '全量売却',
  no_action: '経済的見送り',
  defer: '判断保留',
}

export function proposalActionLabel(item: SavedProposalView['items'][number]) {
  if (item.action === 'buy' && item.terms === null) {
    return item.economic_decision === 'price_conditional_allocation'
      ? '価格条件付きの買付方針（注文条件未確認）'
      : '買付案（保存時の具体条件なし）'
  }
  if (item.action === 'sell' && item.reason_code === 'consider_full_recovery') return '全量回収の条件案'
  if (item.action === 'sell' && item.reason_code === 'consider_partial_recovery') return '一部回収の条件案'
  return ACTION_LABEL[item.action]
}

export function publicationPeriod(publishedAt: string) {
  const parts = jstDateTimeParts(new Date(publishedAt))
  const period = Number(parts.hour) < 12 ? '午前' : '午後'
  return {
    key: `${parts.year}-${parts.month}-${parts.day}-${period}`,
    label: `${Number(parts.month)}/${Number(parts.day)} ${period}`,
    year: parts.year,
    time: `${parts.hour}:${parts.minute}`,
  }
}

export function proposalReportHref(proposalId: string, ticker: string): string {
  return `/proposal?${new URLSearchParams({ proposal: proposalId, ticker }).toString()}`
}

export interface ReportEntry {
  proposal: SavedProposalView
  item: SavedProposalView['items'][number]
}

// An excerpt can discard a final 「ただし…」 or attach another ticker's decision to this one.
function shortWhole(text: string | null | undefined): string | null {
  const value = text?.trim().replace(/\s+/gu, ' ')
  return value && [...value].length <= 90 ? value : null
}

export function reportListSummary({ proposal, item }: ReportEntry): string {
  const authored = shortWhole(item.rationale) ?? (proposal.items.length === 1 ? shortWhole(proposal.headline) : null)
  if (authored !== null) return authored

  // Long and older records still have the item's saved decision. Never amend the report.
  switch (item.action) {
    case 'defer':
      return shortWhole(item.blockers[0]?.message) ?? '判断に必要な条件がそろわず保留。確認事項は個別レポートに記録。'
    case 'buy': {
      const action = item.timing === 'wait_limit' ? '指値で待つ案' : '買い指値案'
      if (item.terms != null) return `保存時の${action}：${item.terms.quantity}株。`
      return item.proposed_limit_yen == null ? '注文の具体条件は未確定。' : `作者が示した価格上限は${item.proposed_limit_yen}円。注文の指値・数量は未確定。`
    }
    case 'no_action':
      // A saved allocation decision does not establish its method or what the person did.
      return '保存時の判断は新規購入の見送り。理由は個別レポートで確認できます。'
    case 'hold':
      return item.holding_basis === 'investment_case_intact' ? '今回の保有仮説を点検し、保有を継続。' : '今後の見返りが十分と判断し、保有を継続。'
    case 'sell':
      if (item.reason_code === 'consider_partial_recovery' || item.reason_code === 'consider_full_recovery') {
        return `${proposalActionLabel(item)}。保有継続との比較で、注文条件は提示していない。`
      }
      return item.reason_code === 'economic_invalidation' ? '重要な投資仮説が成立しなくなったため、全量売却を提案。' : '保有を続ける見返りが足りないため、全量売却を提案。'
  }
}

type ListedReport = ReportEntry & { publicationTime: string; corrected: boolean }
type PublicationGroup = Omit<ReturnType<typeof publicationPeriod>, 'time'> & { reports: ListedReport[] }

export function groupProposalReports(proposals: SavedProposalView[]) {
  // The saved reader orders aware publication instants (including microseconds), not market as_of.
  const groups = new Map<string, PublicationGroup>()
  for (const proposal of proposals) {
    const { key, label, year, time } = publicationPeriod(proposal.published_at)
    let group = groups.get(key)
    if (group === undefined) {
      group = { key, label, year, reports: [] }
      groups.set(key, group)
    }
    const corrected = isCorrectedProposal(proposal)
    for (const item of proposal.items) group.reports.push({ proposal, item, publicationTime: time, corrected })
  }
  return [...groups.values()]
}

export function isCorrectedProposal(proposal: SavedProposalView): boolean {
  return proposal.corrections.some(correction => correction.erroneous_ref.proposal_id === proposal.proposal_id)
}
