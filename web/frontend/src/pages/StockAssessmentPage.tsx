import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router'

import { fetchJson } from '../api/client'
import type { StockAssessmentDetailView } from '../api/types'
import { AsOfBadge } from '../components/AsOfBadge'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageShell } from '../components/PageShell'
import { PageState } from '../components/PageState'
import { ResearchNav } from '../components/ResearchNav'
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
  if (error) return <PageState message={error} title="銘柄評価" />
  if (!data) return <LoadingPage label="銘柄評価を読み込んでいます" />
  const ticker = data.revision.ticker
  return <PageShell above={<ResearchNav />} title={`${ticker} · 保存した銘柄評価`} lead="この評価版の事業理解・根拠・価値見積りです。提案時や現在の価格・資金へ付け替えません。" width="reading" meta={<div className="flex flex-wrap gap-2"><Badge variant="outline">{STANCE_LABELS[data.revision.stance] ?? data.revision.stance}</Badge><AsOfBadge value={data.revision.as_of} /><UpdatedAtBadge value={data.revision.published_at} /></div>}>
    <SectionCard padded title="企業評価の根拠"><p className="mb-4 text-sm">原評価の購入上限（Pmax・丸め前） <YenAmount value={data.revision.pmax_original_yen} /></p><ReviewedAssessmentContent document={data.document} projection={data.projection} review={data.review} /></SectionCard>
    <SectionCard padded title="独立 Review"><p className="break-all text-xs">Assessment: {data.revision.assessment_id} · Review: {data.revision.review_id}</p><pre className="mt-3 overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify(data.review, null, 2)}</pre></SectionCard>
    <Link className="text-sm underline" to={`/securities/${ticker}`}>最新の銘柄情報・評価を見る →</Link>
    <Link className="text-sm underline" to="/stocks">提案一覧に戻る</Link>
  </PageShell>
}
