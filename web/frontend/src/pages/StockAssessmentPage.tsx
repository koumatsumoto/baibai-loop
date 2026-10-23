import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router'

import { fetchJson } from '../api/client'
import type { StockAssessmentDetailView } from '../api/types'
import { AsOfBadge } from '../components/AsOfBadge'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageShell } from '../components/PageShell'
import { PageState } from '../components/PageState'
import { ResearchNav } from '../components/ResearchNav'
import { AssessmentReturnLink } from '../components/ReadingNavigation'
import { ReviewedAssessmentContent } from '../components/ReviewedAssessmentContent'
import { SectionCard } from '../components/SectionCard'
import { UpdatedAtBadge } from '../components/UpdatedAtBadge'
import { YenAmount } from '../components/YenAmount'
import { Badge } from '../components/ui/badge'
import { STANCE_LABELS } from '../lib/research'

export function StockAssessmentPage() {
  const { assessmentId = '' } = useParams<{ assessmentId: string }>()
  const [data, setData] = useState<StockAssessmentDetailView | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    setData(null)
    setError(null)
    fetchJson<StockAssessmentDetailView>(`/api/stock-assessments/${encodeURIComponent(assessmentId)}`, { signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) setData(result) })
      .catch((reason: unknown) => {
        if (controller.signal.aborted || (reason instanceof Error && reason.name === 'AbortError')) return
        setError(reason instanceof Error ? reason.message : '銘柄評価を読み込めませんでした')
      })
    return () => controller.abort()
  }, [assessmentId])
  if (error) return <><AssessmentReturnLink /><PageState message={error} title="銘柄評価" /></>
  if (!data || data.revision.assessment_id !== assessmentId) return <><AssessmentReturnLink /><LoadingPage label="銘柄評価を読み込んでいます" /></>
  return <SavedStockAssessment data={data} />
}

export function SavedStockAssessment({ data }: { data: StockAssessmentDetailView }) {
  const ticker = data.revision.ticker
  return <PageShell above={<div className="grid gap-2"><ResearchNav /><AssessmentReturnLink ticker={ticker} /></div>} title={`${data.revision.company_name ? `${data.revision.company_name} · ` : ''}${ticker} · 保存した銘柄評価`} lead="この評価版の事業理解・根拠・価値見積りです。提案時や現在の価格・資金へ付け替えません。" width="reading" meta={<div className="flex flex-wrap gap-2"><Badge variant="outline">{STANCE_LABELS[data.revision.stance] ?? data.revision.stance}</Badge><AsOfBadge value={data.revision.as_of} /><UpdatedAtBadge value={data.revision.published_at} /></div>}>
    <SectionCard padded title="企業評価の根拠"><p className="mb-4 text-sm">原評価の条件付き購入上限（Pmax） <YenAmount value={data.revision.pmax_original_yen} /></p><p className="mb-4 text-xs text-muted-foreground">保存した仮定と要求率に基づく計算値です。仮説の成立、購入の推奨、現在の注文条件を示しません。</p><ReviewedAssessmentContent document={data.document} projection={data.projection} review={data.review} /></SectionCard>
    <details className="min-w-0 rounded border p-4"><summary className="min-h-11 cursor-pointer py-2 text-sm">この固定評価・Reviewの識別子</summary><div className="mt-3 grid gap-2 break-all text-xs"><p>Assessment: {data.revision.assessment_id}</p><p>Review: {data.revision.review_id}</p><p>core hash: {data.core_sha256}</p></div></details>
    <Link className="inline-flex min-h-11 w-fit items-center text-sm underline" to={`/securities/${ticker}`}>最新の銘柄情報・評価を見る（この固定版とは別） →</Link>
    <AssessmentReturnLink ticker={ticker} />
  </PageShell>
}
