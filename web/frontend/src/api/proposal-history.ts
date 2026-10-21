import { fetchJson } from './client'
import type { InvestmentProposalsView, InvestmentProposalSummaryView } from './types'

export async function fetchProposalHistory(signal: AbortSignal): Promise<InvestmentProposalsView> {
  const proposals: InvestmentProposalSummaryView[] = []
  let cursor: string | null = null
  do {
    const cursorQuery = cursor === null ? '' : `?cursor=${encodeURIComponent(cursor)}`
    const page: InvestmentProposalsView = await fetchJson<InvestmentProposalsView>(`/api/investment-proposals${cursorQuery}`, { signal })
    proposals.push(...page.proposals)
    cursor = page.next_cursor
  } while (cursor !== null && !signal.aborted)
  return { proposals, next_cursor: cursor }
}

