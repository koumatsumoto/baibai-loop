import type { MacroContextView, MacroCoreSectionView } from '../../api/types'
import { Card, CardDescription, CardHeader, CardTitle } from '../../components/ui/card'
import { formatJstDateTime } from '../../lib/format'
import { labelMacroValue, macroTone } from '../../lib/macro-report'
import { ReportToneBadge } from '../../components/report/ReportToneBadge'
import { EvidenceDisclosure, JudgmentBadge } from './shared'
import { ScenarioDistribution } from './ScenarioDistribution'

export function MacroReportOverview({ data, regime, risk }: { data: MacroContextView; regime: MacroCoreSectionView | null; risk: MacroCoreSectionView | null }) {
  const riskEnvironment = risk?.risk_environment
  return (
    <section aria-labelledby="macro-overview-title" className="grid gap-4">
      <Card className="shadow-sm">
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle aria-level={2} id="macro-overview-title" role="heading">現局面</CardTitle>
          </div>
          <CardDescription className="break-all">
            公開 {formatJstDateTime(data.published_at)} · {data.context_id}
          </CardDescription>
        </CardHeader>
        <div className="grid gap-5 px-4 pb-4 sm:px-6">
          <p className="max-w-4xl text-base leading-7">{data.summary}</p>
          {regime?.change_since_previous && <div className="rounded-lg border-l-4 border-primary bg-muted/45 p-4 text-sm"><h3 className="font-semibold">前回からの変化</h3><p className="mt-1">{regime.change_since_previous}</p></div>}
          {regime && (
            <div className="grid gap-2 rounded-lg bg-muted/45 p-4">
              <div className="flex flex-wrap items-center gap-2"><h3 className="font-semibold">中心像</h3><JudgmentBadge confidence={regime.judgment.confidence} direction={regime.judgment.direction} /></div>
              <p>{regime.judgment.summary}</p>
              <EvidenceDisclosure ids={regime.judgment.source_ids} />
            </div>
          )}
          {riskEnvironment && (
            <div className="grid gap-2 rounded-lg border p-4">
              <div className="flex flex-wrap items-center gap-2"><h3 className="font-semibold">リスク環境</h3><ReportToneBadge tone={macroTone(riskEnvironment.stance)}>{labelMacroValue(riskEnvironment.stance)}</ReportToneBadge><span className="text-xs text-muted-foreground">確信度 {labelMacroValue(riskEnvironment.confidence)}</span></div>
              <p>{riskEnvironment.summary}</p>
              <p className="text-xs text-muted-foreground">共通の出典は、後段の反証条件・シナリオ欄で確認できます。</p>
            </div>
          )}
          <ScenarioDistribution scenarios={risk?.scenarios} />
          {regime?.previous_scorecard_review && <div className="rounded-lg border p-4 text-sm"><h3 className="font-semibold">前回シナリオの確認</h3><p className="mt-1">{regime.previous_scorecard_review}</p></div>}
        </div>
      </Card>
    </section>
  )
}
