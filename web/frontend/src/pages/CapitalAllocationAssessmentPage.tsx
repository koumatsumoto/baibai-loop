import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router'

import { fetchJson } from '../api/client'
import type { CapitalAllocationAssessmentView } from '../api/types'
import { LoadingPage } from '../components/LoadingIndicator'
import { AsOfBadge } from '../components/AsOfBadge'
import { PageShell } from '../components/PageShell'
import { PageState } from '../components/PageState'
import { SectionCard } from '../components/SectionCard'
import { UpdatedAtBadge } from '../components/UpdatedAtBadge'
import { Badge } from '../components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../components/ui/table'
import { formatNumber } from '../lib/format'

export function CapitalAllocationAssessmentPage() {
  const { capitalAllocationAssessmentId = '' } = useParams<{ capitalAllocationAssessmentId: string }>()
  const [data, setData] = useState<CapitalAllocationAssessmentView | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    fetchJson<CapitalAllocationAssessmentView>(`/api/capital-allocation-assessments/${encodeURIComponent(capitalAllocationAssessmentId)}`).then(setData).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : 'Capital Allocation Assessment を読み込めませんでした'))
  }, [capitalAllocationAssessmentId])
  if (error) return <PageState message={error} title="Capital Allocation Assessment" />
  if (!data) return <LoadingPage label="Capital Allocation Assessment を読み込んでいます" />
  return (
    <PageShell lead="Research Set の thesis を同じ時点で比較し、資本を配分するか見送るかを記録した判断です。" meta={<div className="flex flex-wrap gap-3 text-xs text-muted-foreground"><Badge>{data.decision}</Badge><UpdatedAtBadge value={data.published_at} /><AsOfBadge value={data.as_of} /></div>} title="Capital Allocation Assessment" width="reading">
      <SectionCard padded title={data.headline}><p className="whitespace-pre-line text-sm">{data.comparison}</p></SectionCard>
      <SectionCard description="thesis の scalar は保存せず、表示時に bound revision から投影" padded title="Alternatives">
        <Table><TableHeader><TableRow><TableHead>銘柄</TableHead><TableHead>判断</TableHead><TableHead>理由</TableHead><TableHead className="text-right">原評価の Base 年率換算</TableHead><TableHead className="text-right">Pmax（原評価）</TableHead><TableHead>投資理由の成立性</TableHead></TableRow></TableHeader><TableBody>{data.alternatives.map((item) => <TableRow key={item.ticker}><TableCell><Link className="font-mono font-semibold hover:underline" to={`/securities/${item.ticker}`}>{item.ticker}</Link></TableCell><TableCell><Badge variant={item.disposition === 'allocate' ? 'default' : 'secondary'}>{item.disposition}</Badge></TableCell><TableCell>{item.rationale}</TableCell><TableCell className="text-right font-mono">{item.base_annualized_return_pct === null ? '—' : `${formatNumber(item.base_annualized_return_pct, 1)}%`}</TableCell><TableCell className="text-right font-mono">{item.pmax_raw_yen === null ? '—' : `${formatNumber(item.pmax_raw_yen, 0)}円`}</TableCell><TableCell>{item.case_status ?? '—'}</TableCell></TableRow>)}</TableBody></Table>
      </SectionCard>
      <SectionCard padded title="Foregone alternatives"><p className="whitespace-pre-line text-sm">{data.foregone_alternatives}</p></SectionCard>
      <p className="text-xs text-muted-foreground">Research Triage: {data.research_triage_id} · review: {data.content_review.conclusion}</p>
    </PageShell>
  )
}
