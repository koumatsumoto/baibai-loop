import type { MacroContextView, MacroCoreSectionView } from '../../api/types'
import { StaleBadge } from '../../components/StaleBadge'
import { Badge } from '../../components/ui/badge'
import { Card, CardDescription, CardHeader, CardTitle } from '../../components/ui/card'
import { formatJstDateTime } from '../../lib/format'
import { labelMacroValue } from '../../lib/macro-report'
import { JudgmentBadge, SourceIds } from './shared'

export function MacroReportOverview({ data, regime, risk }: { data: MacroContextView; regime: MacroCoreSectionView | null; risk: MacroCoreSectionView | null }) {
  const riskEnvironment = risk?.risk_environment
  return (
    <section aria-labelledby="macro-overview-title" className="grid gap-4">
      <Card className="shadow-sm">
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle aria-level={2} id="macro-overview-title" role="heading">現局面</CardTitle>
            {data.stale && <StaleBadge />}
          </div>
          <CardDescription className="break-all">
            基準日 {data.as_of}（{data.age_days} 日前） · 公開 {formatJstDateTime(data.published_at)} · {data.context_id}
          </CardDescription>
        </CardHeader>
        <div className="grid gap-5 px-4 pb-4 sm:px-6">
          <p className="max-w-4xl text-base leading-7">{data.summary}</p>
          {regime && (
            <div className="grid gap-2 rounded-lg bg-muted/45 p-4">
              <div className="flex flex-wrap items-center gap-2"><h3 className="font-semibold">中心像</h3><JudgmentBadge confidence={regime.judgment.confidence} direction={regime.judgment.direction} /></div>
              <p>{regime.judgment.summary}</p>
              <SourceIds ids={regime.judgment.source_ids} />
              {regime.change_since_previous && <p className="border-t pt-3 text-sm"><span className="font-medium">前回からの変化:</span> {regime.change_since_previous}</p>}
              {regime.previous_scorecard_review && <p className="text-sm"><span className="font-medium">前回シナリオの確認:</span> {regime.previous_scorecard_review}</p>}
            </div>
          )}
          {riskEnvironment && (
            <div className="grid gap-2 rounded-lg border p-4">
              <div className="flex flex-wrap items-center gap-2"><h3 className="font-semibold">リスク環境</h3><Badge variant="secondary">{labelMacroValue(riskEnvironment.stance)} / 確信度 {labelMacroValue(riskEnvironment.confidence)}</Badge></div>
              <p>{riskEnvironment.summary}</p>
              <p className="text-xs text-muted-foreground">共通の出典は、後段の反証条件・シナリオ欄で確認できます。</p>
            </div>
          )}
        </div>
      </Card>
    </section>
  )
}
