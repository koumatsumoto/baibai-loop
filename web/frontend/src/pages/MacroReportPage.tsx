import { useEffect, useState } from 'react'
import { ArrowLeft } from 'lucide-react'
import { Link, useParams } from 'react-router'

import { fetchJson } from '../api/client'
import type { MacroContextView } from '../api/types'
import { LoadingPage } from '../components/LoadingIndicator'
import { AsOfBadge } from '../components/AsOfBadge'
import { PageShell } from '../components/PageShell'
import { PageState } from '../components/PageState'
import { StaleBadge } from '../components/StaleBadge'
import { UpdatedAtBadge } from '../components/UpdatedAtBadge'
import { Button } from '../components/ui/button'
import { MacroReportContent } from './macro-report/MacroReportContent'

export function MacroReportPage() {
  const { contextId = '' } = useParams<{ contextId: string }>()
  const [data, setData] = useState<MacroContextView | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    setData(null)
    setError(null)
    fetchJson<MacroContextView>(`/api/macro/context/${encodeURIComponent(contextId)}`, { signal: controller.signal })
      .then((result) => {
        if (!controller.signal.aborted) setData(result)
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted || (reason instanceof Error && reason.name === 'AbortError')) return
        setError(reason instanceof Error ? reason.message : 'レポートを読み込めませんでした')
      })
    return () => controller.abort()
  }, [contextId])

  if (error) return <PageState message={error} title="Macro report" />
  if (!data) return <LoadingPage label="レポートを読み込んでいます" />

  return (
    <PageShell
      above={<div><Button asChild size="sm" variant="ghost"><Link to="/macro"><ArrowLeft />Macro に戻る</Link></Button></div>}
      meta={<div className="flex flex-wrap items-center gap-2"><UpdatedAtBadge value={data.published_at} /><AsOfBadge compact value={data.as_of} /><span className="text-xs text-muted-foreground">{data.age_days}日前</span>{data.stale && <StaleBadge />}</div>}
      title="マクロ環境レポート"
      width="reading"
    >
      <MacroReportContent data={data} />
    </PageShell>
  )
}
