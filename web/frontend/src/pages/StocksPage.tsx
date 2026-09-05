import { useEffect, useState } from 'react'
import { Link } from 'react-router'

import { fetchJson } from '../api/client'
import type { ScreeningView } from '../api/types'
import { AsOfBadge } from '../components/AsOfBadge'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageShell } from '../components/PageShell'
import { PageState } from '../components/PageState'
import { SectionCard } from '../components/SectionCard'
import { UpdatedAtBadge } from '../components/UpdatedAtBadge'
import { Badge } from '../components/ui/badge'
import { ReviewSetSection } from './stocks/ReviewSetSection'
import { SecurityAnalysisSection } from './stocks/SecurityAnalysisSection'

export function StocksPage() {
  const [data, setData] = useState<ScreeningView | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    fetchJson<ScreeningView>('/api/screening/latest').then(setData).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : 'Stocks を読み込めませんでした'))
  }, [])
  if (error) return <PageState message={error} title="Stocks read error" />
  if (!data) return <LoadingPage label="価値分析を読み込んでいます" />
  if (!data.run) return <PageState message="screening run publication がありません" title="Stocks" />
  const latestReviewSet = [...data.review_sets].sort((left, right) => left.created_at.localeCompare(right.created_at)).at(-1) ?? null
  return (
    <PageShell
      lead="Review Setはfreeze済みの選定時座標、Security Analysisは全分析母集団の探索面です。E[r]は参考であり、Review Setの採否や順序には使いません。"
      meta={<div className="flex flex-wrap gap-4"><UpdatedAtBadge value={data.run.generated_at} /><AsOfBadge compact value={data.run.as_of} /><span className="font-mono text-xs text-muted-foreground">分析 {data.run.analyzed_security_count.toLocaleString('ja-JP')} 銘柄</span></div>}
      title="Stocks"
    >
      <SectionCard description="人間が Research / Skip を判断した発行済み記録" padded title="Research Triage">
        {data.research_triages.length === 0 ? <p className="text-sm text-muted-foreground">Research Triage はまだ publish されていません。</p> : <div className="flex flex-wrap gap-2">{data.research_triages.map((item) => <Link className="min-w-0 max-w-full" key={item.research_triage_id} to="/research-triage"><Badge className="max-w-full truncate" variant="secondary">{item.research_triage_id}</Badge></Link>)}</div>}
      </SectionCard>
      <SectionCard description="Research Set の thesis を比較した資本配分判断" padded title="Capital Allocation Assessment">
        {data.capital_allocation_assessments.length === 0 ? <p className="text-sm text-muted-foreground">Capital Allocation Assessment はまだ publish されていません。</p> : <div className="grid gap-2">{data.capital_allocation_assessments.map((item) => <Link className="rounded-md border p-3 hover:bg-muted/40" key={item.capital_allocation_assessment_id} to={`/stocks/capital-allocation-assessments/${item.capital_allocation_assessment_id}`}><div className="flex flex-wrap items-center gap-2"><Badge>{item.decision}</Badge><strong>{item.headline}</strong>{item.allocated_ticker && <span className="font-mono">{item.allocated_ticker}</span>}</div><p className="mt-1 text-xs text-muted-foreground">基準 {item.as_of} · alternatives {item.alternative_count}</p></Link>)}</div>}
      </SectionCard>
      <ReviewSetSection reviewSet={latestReviewSet} runAsOf={data.run.as_of} />
      <SecurityAnalysisSection rows={data.security_analyses} runAsOf={data.run.as_of} />
    </PageShell>
  )
}
