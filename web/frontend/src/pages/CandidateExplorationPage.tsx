import { useEffect, useState } from 'react'
import { fetchJson } from '../api/client'
import type { ScreeningView } from '../api/types'
import { AsOfBadge } from '../components/AsOfBadge'
import { UpdatedAtBadge } from '../components/UpdatedAtBadge'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageShell } from '../components/PageShell'
import { PageState } from '../components/PageState'
import { ResearchNav } from '../components/ResearchNav'
import { ReviewSetSection } from './stocks/ReviewSetSection'
import { SecurityAnalysisSection } from './stocks/SecurityAnalysisSection'

export function CandidateExplorationPage() {
  const [data, setData] = useState<ScreeningView | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    fetchJson<ScreeningView>('/api/screening/latest').then(setData).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : '候補を読み込めませんでした'))
  }, [])
  if (error) return <PageState message={error} title="候補探索" />
  if (!data) return <LoadingPage label="候補を読み込んでいます" />
  const reviewSet = [...data.review_sets].sort((a, b) => a.created_at.localeCompare(b.created_at)).at(-1) ?? null
  return <PageShell above={<ResearchNav />} title="候補探索" lead="全銘柄と保存済み外部評価から、調査する企業を探します。機械の候補やE[r]は購入の提案ではありません。" meta={data.run && <div className="flex flex-wrap gap-3"><UpdatedAtBadge value={data.run.generated_at} /><AsOfBadge value={data.run.as_of} /></div>}>
    <SecurityAnalysisSection rows={[...data.security_analyses, ...data.external_assessment_securities]} reviewSet={reviewSet} runAsOf={data.run?.as_of ?? null} externalCutoff={data.external_assessment_cutoff} calibration={data.er_level_calibration} />
    <details className="rounded-lg border bg-card p-4">
      <summary className="cursor-pointer font-medium">選定時の候補と根拠</summary>
      <div className="mt-4 grid gap-4">
        <ReviewSetSection reviewSet={reviewSet} runAsOf={data.run?.as_of ?? '未発行'} />
      </div>
    </details>
  </PageShell>
}
