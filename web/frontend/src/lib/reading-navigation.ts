import { useEffect } from 'react'
import type { MouseEvent } from 'react'
import { useLocation } from 'react-router'
import type { NavigateFunction } from 'react-router'

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

// Put the reading anchor on the existing history entry, not in a new store.
// Browser Back then uses the same post-fetch anchor restoration as the return link.
export function followAssessmentFromProposal(
  event: Pick<MouseEvent, 'button' | 'metaKey' | 'ctrlKey' | 'shiftKey' | 'altKey' | 'defaultPrevented' | 'preventDefault'>,
  navigate: NavigateFunction,
  origin: { pathname: string; search: string; state: unknown },
  ticker: string,
  target: string,
) {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
  event.preventDefault()
  void navigate(`${origin.pathname}${origin.search}#proposal-item-${encodeURIComponent(ticker)}`, { replace: true, state: origin.state })
  void navigate(target)
}
