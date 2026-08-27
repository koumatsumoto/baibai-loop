import { CircleAlert } from 'lucide-react'

import type { MacroCoreSectionView, MacroScenarioView } from '../../api/types'
import { SectionCard } from '../../components/SectionCard'
import { Alert, AlertDescription, AlertTitle } from '../../components/ui/alert'
import { Badge } from '../../components/ui/badge'
import { labelMacroValue, orderMacroScenarios } from '../../lib/macro-report'
import { Disclosure, SourceIds } from './shared'

const COMPARISON_SYMBOLS: Readonly<Record<string, string>> = { below: '<', at_or_below: '≤', above: '>', at_or_above: '≥' }

function ScenarioCard({ scenario }: { scenario: MacroScenarioView }) {
  return (
    <article className="grid min-w-0 content-start gap-3 rounded-lg border p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="font-semibold">{labelMacroValue(scenario.case)} <span className="font-mono text-xs text-muted-foreground">({scenario.case})</span></h3>
        {scenario.probability != null && <Badge variant="outline">主観ウェイト {Math.round(scenario.probability * 100)}%</Badge>}
        <Badge variant="secondary">{labelMacroValue(scenario.direction)}</Badge>
      </div>
      <p>{scenario.summary}</p>
      {(scenario.conditions ?? []).length > 0 && <div><h4 className="text-xs font-semibold">成立条件</h4>{scenario.conditions.map((condition, index) => <p className="mt-1 text-sm text-muted-foreground" key={`condition-${index}`}>{condition}</p>)}</div>}
      {(scenario.economic_implications ?? []).length > 0 && <div><h4 className="text-xs font-semibold">経済経路への含意</h4>{scenario.economic_implications.map((item, index) => <p className="mt-1 text-sm text-muted-foreground" key={`implication-${index}`}>{item}</p>)}</div>}
      {(scenario.scorecard ?? []).length > 0 && <Disclosure label="機械照合条件"><div className="grid gap-1">{scenario.scorecard.map((condition, index) => <p className="break-all font-mono text-[11px] text-muted-foreground tabular-nums" key={`${condition.series_id}-${index}`}>{condition.series_id} {COMPARISON_SYMBOLS[condition.comparison] ?? condition.comparison} {condition.threshold} · 期限 {condition.deadline}</p>)}</div></Disclosure>}
      <SourceIds ids={scenario.source_ids} />
    </article>
  )
}

export function MacroScenariosAndMonitoring({ risk, monitoring }: { risk: MacroCoreSectionView | null; monitoring: MacroCoreSectionView | null }) {
  const riskEnvironment = risk?.risk_environment
  const scenarios = orderMacroScenarios(risk?.scenarios)
  const points = monitoring?.monitoring_points ?? []
  if (!riskEnvironment && scenarios.length === 0 && points.length === 0) return null
  return (
    <SectionCard description="確率は比較のための主観ウェイトです。自動 sizing や期待値計算には使いません。" padded title="シナリオと見方を変える条件">
      <div className="grid gap-5">
        {riskEnvironment && <Alert role="note"><CircleAlert /><AlertTitle>この見方を変える条件</AlertTitle><AlertDescription><p className="mb-2 text-xs">現局面のリスク環境と共通の出典に基づく反証条件です。</p>{riskEnvironment.falsifiers?.length > 0 ? <ul className="grid list-disc gap-1 pl-5">{riskEnvironment.falsifiers.map((falsifier, index) => <li key={`falsifier-${index}`}>{falsifier}</li>)}</ul> : <p>明示された反証条件はありません。</p>}<SourceIds ids={riskEnvironment.source_ids} /></AlertDescription></Alert>}
        {scenarios.length > 0 && <div className="grid gap-3 lg:grid-cols-3">{scenarios.map((scenario) => <ScenarioCard key={scenario.case} scenario={scenario} />)}</div>}
        {points.length > 0 && <div className="grid gap-3"><h3 className="font-semibold">監視する観測点</h3>{points.map((point, index) => <article className="grid gap-2 rounded-lg border p-4" key={`monitor-${index}`}><h4 className="font-medium">{point.event}</h4><p className="text-sm">{point.summary}</p><dl className="grid gap-2 text-sm sm:grid-cols-[8rem_1fr]"><dt className="font-medium">条件</dt><dd>{point.condition}</dd><dt className="font-medium">見方の変更</dt><dd>{point.view_change}</dd></dl><SourceIds ids={point.source_ids} /></article>)}</div>}
      </div>
    </SectionCard>
  )
}
