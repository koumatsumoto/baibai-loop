import type { MacroScenarioView } from '../../api/types'
import { ReportToneBadge } from '../../components/report/ReportToneBadge'
import { labelMacroValue, macroTone, scenarioDistribution } from '../../lib/macro-report'

export function ScenarioDistribution({ scenarios }: { scenarios: readonly MacroScenarioView[] | null | undefined }) {
  const distribution = scenarioDistribution(scenarios)
  if (distribution === null) return null
  return (
    <figure className="grid gap-2 rounded-lg border p-4">
      <figcaption className="font-semibold">見通しの分布（主観ウェイト）</figcaption>
      <div aria-hidden="true" className="flex h-3 overflow-hidden rounded-full bg-muted">
        {distribution.map(({ probability, scenario }) => (
          <span
            className={scenario.case === 'base' ? 'bg-muted-foreground/55' : scenario.case === 'bear' ? 'bg-warning' : 'bg-positive'}
            key={scenario.case}
            style={{ width: `${probability * 100}%` }}
          />
        ))}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-2">
        {distribution.map(({ probability, scenario }) => (
          <span className="inline-flex items-center gap-1.5 text-sm" key={scenario.case}>
            <ReportToneBadge tone={macroTone(scenario.case)}>{labelMacroValue(scenario.case)}</ReportToneBadge>
            <span className="font-mono tabular-nums">{Math.round(probability * 100)}%</span>
          </span>
        ))}
      </div>
      <p className="text-xs text-muted-foreground">3ケースを比較するための主観値です。統計確率や売買signalではありません。</p>
    </figure>
  )
}
