import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router'

import { fetchJson } from '../api/client'
import type { AllocationAlternativeView, CapitalAllocationAssessmentView } from '../api/types'
import { LoadingPage } from '../components/LoadingIndicator'
import { AsOfBadge } from '../components/AsOfBadge'
import { PageShell } from '../components/PageShell'
import { PageState } from '../components/PageState'
import { SectionCard } from '../components/SectionCard'
import { ReviewedThesisContent } from '../components/ReviewedThesisContent'
import { UpdatedAtBadge } from '../components/UpdatedAtBadge'
import { Badge } from '../components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../components/ui/table'
import { formatNumber } from '../lib/format'

function thesisAnchor(item: AllocationAlternativeView): string {
  return `caa-thesis-${item.thesis_id}`
}

export function AllocationThesisDetails({ item }: { item: AllocationAlternativeView }) {
  return <div className="scroll-mt-24" id={thesisAnchor(item)}>
    <SectionCard padded title={`${item.ticker} · CAA参照の企業評価`} description="この配分判断が比較に用いたThesisとReviewです。" meta={<AsOfBadge value={item.valuation_as_of} />}>
      <div className="grid gap-5">
        <p className="break-all text-xs">Thesis: {item.thesis_id} · Review: {item.thesis_review_id ?? '参照未記録'}</p>
        <p>Pmax（原評価） {item.pmax_raw_yen === null ? '未確認' : `${formatNumber(item.pmax_raw_yen, 2)}円`}</p>
        {Object.keys(item.thesis_projection).length === 0 ? <p>このCAAに固定された企業評価の本文を参照できません。</p> : <ReviewedThesisContent projection={item.thesis_projection} />}
        <details className="rounded border p-4"><summary className="cursor-pointer font-medium">CAA参照のReview本文</summary>
          {item.thesis_review === null ? <p className="mt-3 text-sm">{item.thesis_review_id === null ? '参照したReview本文は記録されていません。' : '参照したReview本文を取得できませんでした。'}</p> : <pre className="mt-3 overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify(item.thesis_review, null, 2)}</pre>}
        </details>
        <Link className="text-sm underline" to={`/securities/${item.ticker}`}>最新の銘柄情報・企業評価を見る</Link>
      </div>
    </SectionCard>
  </div>
}

export function CapitalAllocationAssessmentPage() {
  const { capitalAllocationAssessmentId = '' } = useParams<{ capitalAllocationAssessmentId: string }>()
  const [data, setData] = useState<CapitalAllocationAssessmentView | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    setData(null)
    setError(null)
    fetchJson<CapitalAllocationAssessmentView>(`/api/capital-allocation-assessments/${encodeURIComponent(capitalAllocationAssessmentId)}`, { signal: controller.signal })
      .then((result) => {
        if (!controller.signal.aborted) setData(result)
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted || (reason instanceof Error && reason.name === 'AbortError')) return
        setError(reason instanceof Error ? reason.message : 'Capital Allocation Assessment を読み込めませんでした')
      })
    return () => controller.abort()
  }, [capitalAllocationAssessmentId])
  useEffect(() => {
    const item = data?.alternatives.find(item => window.location.hash === `#${encodeURIComponent(thesisAnchor(item))}`)
    if (item) document.getElementById(thesisAnchor(item))?.scrollIntoView()
  }, [data])
  if (error) return <PageState message={error} title="Capital Allocation Assessment" />
  if (!data) return <LoadingPage label="Capital Allocation Assessment を読み込んでいます" />
  return (
    <PageShell lead="発行時に参照したThesis/Reviewを比較し、配分または見送りを記録した判断です。参照評価は後続改訂で置き換えません。" meta={<div className="flex flex-wrap gap-3 text-xs text-muted-foreground"><Badge>{data.decision}</Badge><UpdatedAtBadge value={data.published_at} /><AsOfBadge value={data.as_of} /></div>} title="Capital Allocation Assessment" width="reading">
      <SectionCard padded title={data.headline}><p className="whitespace-pre-line text-sm">{data.comparison}</p></SectionCard>
      <SectionCard description="このCAAが比較した原評価。銘柄から参照時点のThesis/Reviewを読めます。" padded title="Alternatives">
        <Table><TableHeader><TableRow><TableHead>銘柄・参照評価</TableHead><TableHead>判断</TableHead><TableHead>理由</TableHead><TableHead className="text-right">原評価の Base 年率換算</TableHead><TableHead className="text-right">Pmax（原評価）</TableHead><TableHead>投資理由の成立性</TableHead></TableRow></TableHeader><TableBody>{data.alternatives.map((item) => <TableRow key={item.ticker}><TableCell><a className="font-mono font-semibold hover:underline" href={`#${encodeURIComponent(thesisAnchor(item))}`}>{item.ticker}</a></TableCell><TableCell><Badge variant={item.disposition === 'allocate' ? 'default' : 'secondary'}>{item.disposition}</Badge></TableCell><TableCell>{item.rationale}</TableCell><TableCell className="text-right font-mono">{item.base_annualized_return_pct === null ? '—' : `${formatNumber(item.base_annualized_return_pct, 1)}%`}</TableCell><TableCell className="text-right font-mono">{item.pmax_raw_yen === null ? '—' : `${formatNumber(item.pmax_raw_yen, 0)}円`}</TableCell><TableCell>{item.case_status ?? '—'}</TableCell></TableRow>)}</TableBody></Table>
      </SectionCard>
      {data.research_operation_id && <SectionCard padded title="今回の調査範囲" description="完了した企業評価だけを比較しました。調査未完了は経済的な見送りと別に記録します。">
        <p className="text-sm">新規対象 {data.selected_new_target_count}件 · 完了 {data.alternatives.length}件 · 未完了 {data.unfinished_research.length}件</p>
        {data.unfinished_research.map(item => <div key={item.ticker} className="mt-3 text-sm"><p><span className="font-mono">{item.ticker}</span> · {item.reason}</p><p className="break-all text-xs text-muted-foreground">{item.references.join(' · ')}</p></div>)}
        <p className="mt-3 break-all text-xs text-muted-foreground">Operation: {data.research_operation_id}</p>
      </SectionCard>}
      <SectionCard padded title="Foregone alternatives"><p className="whitespace-pre-line text-sm">{data.foregone_alternatives}</p></SectionCard>
      {data.alternatives.map(item => <AllocationThesisDetails item={item} key={item.thesis_id} />)}
      <p className="text-xs text-muted-foreground">Research Triage: {data.research_triage_id} · 配分比較のReview: {data.content_review.conclusion}</p>
    </PageShell>
  )
}
