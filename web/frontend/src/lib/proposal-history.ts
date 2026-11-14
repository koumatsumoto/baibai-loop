import type { SavedProposalView } from '../api/types'

export const ACTION_LABEL: Record<SavedProposalView['items'][number]['action'], string> = {
  buy: '買付（指値）',
  hold: '保有継続',
  sell: '全量売却',
  no_action: '経済的見送り',
  defer: '判断保留',
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
