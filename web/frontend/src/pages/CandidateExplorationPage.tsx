import { useEffect, useState } from 'react'
import { fetchJson } from '../api/client'
import type { ScreeningView } from '../api/types'
import { AsOfBadge } from '../components/AsOfBadge'
import { UpdatedAtBadge } from '../components/UpdatedAtBadge'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageShell } from '../components/PageShell'
import { PageState } from '../components/PageState'
import { InvestmentJudgmentNav } from '../components/InvestmentJudgmentNav'
import { CandidateSection } from './stocks/CandidateSection'
import { StockAnalysisSection } from './stocks/StockAnalysisSection'

export function CandidateExplorationPage() {
  const [data, setData] = useState<ScreeningView | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    fetchJson<ScreeningView>('/api/screening/latest', { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setData(value) })
      .catch((reason: unknown) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '候補を読み込めませんでした') })
    return () => controller.abort()
  }, [])
  if (error) return <PageState message={error} title="候補探索" />
  if (!data) return <LoadingPage label="候補を読み込んでいます" />
  return <PageShell above={<InvestmentJudgmentNav />} title="候補探索" lead="全銘柄と保存済み外部評価から、調査する企業を探します。機械の候補やE[r]は購入の提案ではありません。" meta={data.run && <div className="flex flex-wrap gap-3"><UpdatedAtBadge value={data.run.generated_at} /><AsOfBadge value={data.run.as_of} /></div>}>
    <StockAnalysisSection rows={[...data.stock_analyses, ...data.external_assessment_securities]} runAsOf={data.run?.as_of ?? null} externalCutoff={data.external_assessment_cutoff} calibration={data.er_level_calibration} />
    <details className="rounded-lg border bg-card p-4">
      <summary className="cursor-pointer font-medium">選定時の候補と根拠</summary>
      <div className="mt-4 grid gap-4">
        <CandidateSection rows={data.stock_analyses} run={data.run} method={data.candidate_discovery} />
      </div>
    </details>
  </PageShell>
}
