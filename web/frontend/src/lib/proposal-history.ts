import type { SavedProposalView } from '../api/types'

export const ACTION_LABEL: Record<SavedProposalView['items'][number]['action'], string> = {
  buy: '買付（指値）',
  hold: '保有継続',
  sell: '全量売却',
  no_action: '経済的見送り',
  defer: '判断保留',
}

export function proposalActionLabel(item: SavedProposalView['items'][number]) {
  if (item.action === 'sell' && item.reason_code === 'consider_full_recovery') return '全量回収の条件案'
  if (item.action === 'sell' && item.reason_code === 'consider_partial_recovery') return '一部回収の条件案'
  return ACTION_LABEL[item.action]
}

const publicationClock = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Asia/Tokyo', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
})

export function publicationPeriod(publishedAt: string) {
  const parts = Object.fromEntries(publicationClock.formatToParts(new Date(publishedAt)).map(part => [part.type, part.value]))
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
      if (item.terms != null) {
        const terms = item.terms
        return shortWhole(`${terms.target_session}の${action}：${terms.limit_price_yen}円・${terms.quantity}株。`) ?? `保存時の${action}。価格・数量は個別レポートに記録。`
      }
      return shortWhole(item.proposed_limit_yen && `${action}。作者の上限は${item.proposed_limit_yen}円。具体的な指値・数量は未確定。`) ?? `${action}。具体的な指値・数量は未確定。`
    }
    case 'no_action': {
      const valuation = item.assessment?.valuation
      if (valuation?.status === 'resolved') {
        const base = Number(valuation.base.annualized_return_pct)
        const required = Number(valuation.required_annual_return_pct)
        if (valuation.base.annualized_return_pct?.trim() && valuation.required_annual_return_pct?.trim() && Number.isFinite(base) && Number.isFinite(required)) {
          return `評価時の中心ケースは年率約${base.toFixed(1)}%、要求年率は約${required.toFixed(1)}%。今回は購入を見送り。`
        }
      }
      return '他の選択肢と比較し、今回は新規購入を見送った。'
    }
    case 'hold':
      return item.holding_basis === 'investment_case_intact' ? '今回の保有仮説を点検し、保有を継続。' : '今後の見返りが十分と判断し、保有を継続。'
    case 'sell':
      if (item.reason_code === 'consider_partial_recovery' || item.reason_code === 'consider_full_recovery') {
        return `${proposalActionLabel(item)}。保有継続との比較で、注文条件は提示していない。`
      }
      return item.reason_code === 'economic_invalidation' ? '重要な投資仮説が成立しなくなったため、全量売却を提案。' : '保有を続ける見返りが足りないため、全量売却を提案。'
  }
}

export function groupProposalReports(proposals: SavedProposalView[]) {
  // The saved reader orders aware publication instants (including microseconds), not market as_of.
  const groups = new Map<string, ReturnType<typeof publicationPeriod> & { reports: ReportEntry[] }>()
  for (const proposal of proposals) {
    const period = publicationPeriod(proposal.published_at)
    let group = groups.get(period.key)
    if (group === undefined) {
      group = { ...period, reports: [] }
      groups.set(period.key, group)
    }
    for (const item of proposal.items) group.reports.push({ proposal, item })
  }
  return [...groups.values()]
}

export function isCorrectedProposal(proposal: SavedProposalView): boolean {
  return (proposal.corrections ?? []).some(correction => correction.erroneous_ref.proposal_id === proposal.proposal_id)
}
