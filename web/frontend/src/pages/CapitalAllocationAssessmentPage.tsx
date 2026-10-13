import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router'

import { fetchJson } from '../api/client'
import type { AllocationAlternativeView, CapitalAllocationAssessmentView } from '../api/types'
import { LoadingPage } from '../components/LoadingIndicator'
import { AsOfBadge } from '../components/AsOfBadge'
import { PageShell } from '../components/PageShell'
import { ResearchNav } from '../components/ResearchNav'
import { PageState } from '../components/PageState'
import { SectionCard } from '../components/SectionCard'
import { ReviewedThesisContent } from '../components/ReviewedThesisContent'
import { UpdatedAtBadge } from '../components/UpdatedAtBadge'
import { Badge } from '../components/ui/badge'
import { formatNumber } from '../lib/format'

function thesisAnchor(item: AllocationAlternativeView): string {
  return `caa-thesis-${item.thesis_id}`
}

export function AllocationThesisDetails({ item }: { item: AllocationAlternativeView }) {
  return <details className="scroll-mt-24 rounded-lg border bg-card" id={thesisAnchor(item)}>
    <summary className="cursor-pointer px-4 py-3 font-medium">{item.ticker} · 当時の企業評価とReview · {item.valuation_as_of ?? '基準日未記録'}</summary>
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
  </details>
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
    if (item) {
      const element = document.getElementById(thesisAnchor(item))
      if (element instanceof HTMLDetailsElement) element.open = true
      element?.scrollIntoView()
    }
  }, [data])
  if (error) return <PageState message={error} title="Capital Allocation Assessment" />
  if (!data) return <LoadingPage label="Capital Allocation Assessment を読み込んでいます" />
  return (
    <PageShell above={<ResearchNav />} lead="発行時に参照したThesis/Reviewを比較し、配分または見送りを記録した判断です。参照評価は後続改訂で置き換えません。" meta={<div className="flex flex-wrap gap-3 text-xs text-muted-foreground"><Badge>{data.decision}</Badge><UpdatedAtBadge value={data.published_at} /><AsOfBadge value={data.as_of} /></div>} title="配分提案・見送りの根拠" width="reading">
      <SectionCard padded title={data.headline}><p className="whitespace-pre-line text-sm">{data.comparison}</p></SectionCard>
      <SectionCard description="この配分判断が参照した原評価です。銘柄から当時のThesisとReviewへ進めます。" padded title="比較した企業評価">
        <div className="grid gap-3">{data.alternatives.map(item => <article className="grid gap-3 rounded-lg border p-4" key={item.thesis_id}>
          <div className="flex flex-wrap items-center justify-between gap-2"><a className="font-mono font-semibold underline" href={`#${encodeURIComponent(thesisAnchor(item))}`} onClick={() => { const element = document.getElementById(thesisAnchor(item)); if (element instanceof HTMLDetailsElement) element.open = true }}>{item.ticker} · 当時の評価を見る →</a><Badge variant={item.disposition === 'allocate' ? 'default' : 'secondary'}>{item.disposition === 'allocate' ? '配分の提案' : item.disposition === 'decline' ? '見送り' : item.disposition}</Badge></div>
          <p className="text-sm">{item.rationale}</p>
          <dl className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-3">
            <div><dt className="text-muted-foreground">原評価のBase年率換算</dt><dd className="mt-1 font-mono">{item.base_annualized_return_pct === null ? '未確認' : `${formatNumber(item.base_annualized_return_pct, 1)}%`}</dd></div>
            <div><dt className="text-muted-foreground">Pmax（原評価）</dt><dd className="mt-1 font-mono">{item.pmax_raw_yen === null ? '未確認' : `${formatNumber(item.pmax_raw_yen, 2)}円`}</dd></div>
            <div><dt className="text-muted-foreground">投資理由の成立性</dt><dd className="mt-1">{item.case_status ?? '未確認'}</dd></div>
          </dl>
        </article>)}</div>
      </SectionCard>
      {data.research_operation_id && <SectionCard padded title="今回の調査範囲" description="完了した企業評価だけを比較しました。調査未完了は経済的な見送りと別に記録します。">
        <p className="text-sm">新規対象 {data.selected_new_target_count}件 · 完了 {data.alternatives.length}件 · 未完了 {data.unfinished_research.length}件</p>
        {data.unfinished_research.map(item => <div key={item.ticker} className="mt-3 text-sm"><p><span className="font-mono">{item.ticker}</span> · {item.reason}</p><p className="break-all text-xs text-muted-foreground">{item.references.join(' · ')}</p></div>)}
        <p className="mt-3 break-all text-xs text-muted-foreground">Operation: {data.research_operation_id}</p>
      </SectionCard>}
      <SectionCard padded title="他の選択肢を見送った理由"><p className="whitespace-pre-line text-sm">{data.foregone_alternatives}</p></SectionCard>
      {data.alternatives.map(item => <AllocationThesisDetails item={item} key={item.thesis_id} />)}
      {data.market_context_assessment_id && <Link className="text-sm underline" to={`/market-context/assessments/${data.market_context_assessment_id}`}>この配分判断が参照した市場環境判断を見る →</Link>}
      <details className="text-xs text-muted-foreground"><summary className="cursor-pointer">判断の参照記録</summary><p className="mt-2 break-all">Research Triage: {data.research_triage_id} · 配分比較のReview: {data.content_review.conclusion}</p></details>
      <Link className="text-sm underline" to="/stocks">提案一覧に戻る</Link>
    </PageShell>
  )
}
