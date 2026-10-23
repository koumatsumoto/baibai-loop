import type { ReactNode } from 'react'
import { Link, useLocation, useNavigate } from 'react-router'
import { assessmentReturnHref, followAssessmentFromProposal, savedAssessmentHref } from '../lib/reading-navigation'

export function AssessmentReturnLink({ ticker }: { ticker?: string }) {
  const { search } = useLocation()
  const to = assessmentReturnHref(search, ticker)
  return <Link className="inline-flex min-h-11 w-fit items-center rounded px-2 text-sm underline underline-offset-4" to={to}>
    {to === '/stocks' ? '提案一覧に戻る' : ticker ? '元の提案・この銘柄の判断に戻る' : '元の提案に戻る'}
  </Link>
}

export function SavedAssessmentLink({ assessmentId, proposalId, ticker, className, children }: { assessmentId: string; proposalId?: string; ticker: string; className?: string; children: ReactNode }) {
  const location = useLocation()
  const navigate = useNavigate()
  const to = savedAssessmentHref(assessmentId, proposalId)
  return <Link className={className} to={to} onClick={event => {
    if (proposalId) followAssessmentFromProposal(event, navigate, location, ticker, to)
  }}>{children}</Link>
}
