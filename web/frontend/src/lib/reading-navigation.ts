import { useEffect } from 'react'
import { useLocation } from 'react-router'

export function savedAssessmentHref(assessmentId: string, proposalId?: string): string {
  const path = `/stocks/stock-assessments/${encodeURIComponent(assessmentId)}`
  return proposalId ? `${path}?${new URLSearchParams({ proposal: proposalId })}` : path
}

export function assessmentReturnHref(search: string, ticker?: string): string {
  const proposalId = new URLSearchParams(search).get('proposal')
  return proposalId
    ? `/stocks/investment-proposals/${encodeURIComponent(proposalId)}${ticker ? `#proposal-item-${encodeURIComponent(ticker)}` : ''}`
    : '/stocks'
}

// The saved proposal is fetched asynchronously, so its item may not exist when
// browser navigation first applies the fragment. Run again when it is ready.
export function useProposalReadingAnchor(ready: boolean) {
  const { hash } = useLocation()
  useEffect(() => {
    if (!ready || !hash || typeof document === 'undefined') return
    const target = document.getElementById(hash.slice(1))
    if (!target) return
    target.scrollIntoView({ block: 'start' })
    target.focus({ preventScroll: true })
  }, [ready, hash])
}
