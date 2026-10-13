import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { fetchJson } from '../api/client'
import type { CapitalAllocationAssessmentSummaryView, CapitalAllocationAssessmentsView, DashboardView, OperationsView } from '../api/types'
import { AsOfBadge } from '../components/AsOfBadge'
import { UpdatedAtBadge } from '../components/UpdatedAtBadge'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageShell } from '../components/PageShell'
import { ResearchNav } from '../components/ResearchNav'
import { SectionCard } from '../components/SectionCard'
import { Badge } from '../components/ui/badge'
import { HoldingProposals, ResearchOperationCard } from './research/ResearchOperationCard'

export function AllocationProposals({ assessments, error }: { assessments: CapitalAllocationAssessmentSummaryView[]; error?: string | null }) {
  const cards = [...assessments].sort((a, b) => b.published_at.localeCompare(a.published_at)).map(item => <Link className="grid gap-2 rounded-lg border p-4 transition-colors hover:bg-muted/40" key={item.capital_allocation_assessment_id} to={`/stocks/capital-allocation-assessments/${item.capital_allocation_assessment_id}`}>
    <div className="flex flex-wrap items-center gap-2"><Badge variant={item.decision === 'allocate' ? 'default' : 'secondary'}>{{ allocate: '新規配分の提案', no_allocation: '配分見送り', defer: '判断保留' }[item.decision] ?? item.decision}</Badge><AsOfBadge value={item.as_of} /><UpdatedAtBadge value={item.published_at} /></div>
    <strong className="font-medium">{item.headline}</strong>
    <p className="text-xs text-muted-foreground">{item.allocated_ticker ? `対象 ${item.allocated_ticker} · ` : ''}比較した企業評価 {item.alternative_count}件 · 当時の根拠を見る →</p>
  </Link>)
  return <SectionCard title="新規配分の提案・見送り" description="保存した当時の判断です。人間の採否・注文・約定は別工程です。" padded>
    {error ? <p role="alert" className="text-sm text-destructive">提案一覧を読み込めませんでした。提案がないことを意味しません。</p> : cards.length === 0 ? <p className="text-sm text-muted-foreground">表示できる配分提案はありません。調査の進行・未完了は下で確認できます。</p> : <div className="grid gap-3">{cards.slice(0, 3)}{cards.length > 3 && <details><summary className="cursor-pointer text-sm">過去の配分判断（{cards.length - 3}件）</summary><div className="mt-3 grid gap-3">{cards.slice(3)}</div></details>}</div>}
  </SectionCard>
}

export function ResearchPage() {
  const [data, setData] = useState<CapitalAllocationAssessmentsView | null>(null)
  const [names, setNames] = useState<ReadonlyMap<string, string | null>>(new Map())
  const [operations, setOperations] = useState<OperationsView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [operationError, setOperationError] = useState<string | null>(null)
  useEffect(() => {
    fetchJson<CapitalAllocationAssessmentsView>('/api/capital-allocation-assessments').then(setData).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : '提案を読み込めませんでした'))
    fetchJson<DashboardView>('/api/dashboard').then(view => setNames(new Map(view.holdings.map(item => [item.ticker, item.company_name])))).catch(() => {})
    fetchJson<OperationsView>('/api/operations').then(setOperations).catch((reason: unknown) => setOperationError(reason instanceof Error ? reason.message : '調査進捗を読み込めませんでした'))
  }, [])
  if (!data && !error) return <LoadingPage label="提案を読み込んでいます" />
  const hasHoldingProposal = operations?.operations.some(operation => operation.research?.targets.some(target => target.purpose === 'holding' && target.outcome_status === 'proposal')) ?? false
  return <PageShell above={<ResearchNav />} title="調査・提案" lead="完成した提案と、調査の進行を確認します。開始・再開は明示的に依頼し、採否と注文は人間が決めます。">
    <div className={`grid items-start gap-6 ${hasHoldingProposal ? 'lg:grid-cols-2' : ''}`}><AllocationProposals assessments={data?.assessments ?? []} error={error} />
    {operations && <HoldingProposals operations={operations.operations} names={names} />}</div>
    {operationError ? <p role="alert" className="text-sm text-destructive">調査進捗を読み込めませんでした。記録がないことを意味しません。</p> : operations ? <ResearchOperationCard operations={operations.operations} names={names} /> : <p className="text-sm text-muted-foreground">調査進捗を読み込んでいます。</p>}
    <div className="flex flex-wrap gap-4 text-sm"><Link className="underline" to="/stocks/explore">調査候補を探す</Link><Link className="underline" to="/market-context">市場・業種環境の根拠を見る</Link></div>
  </PageShell>
}
