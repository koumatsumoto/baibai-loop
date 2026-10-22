import { useEffect } from 'react'
import { Link, useLocation } from 'react-router'

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

export function AssessmentReturnLink({ ticker }: { ticker?: string }) {
  const { search } = useLocation()
  const to = assessmentReturnHref(search, ticker)
  return <Link className="inline-flex min-h-11 w-fit items-center rounded px-2 text-sm underline underline-offset-4" to={to}>
    {to === '/stocks' ? '提案一覧に戻る' : ticker ? '元の提案・この銘柄の判断に戻る' : '元の提案に戻る'}
  </Link>
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
