import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ArrowLeft, CircleAlert } from 'lucide-react'

import { fetchJson } from '../api/client'
import type { MacroConnectionSectionView, MacroContextView, MacroCoreSectionView, MacroScenarioView, MacroSeriesReferenceView, MacroSynthesisView } from '../api/types'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageShell } from '../components/PageShell'
import { SectionCard } from '../components/SectionCard'
import { PageState } from '../components/PageState'
import { StaleBadge } from '../components/StaleBadge'
import { Alert, AlertDescription, AlertTitle } from '../components/ui/alert'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Card, CardDescription, CardHeader, CardTitle } from '../components/ui/card'
import { formatJstDateTime } from '../lib/format'
import { LABEL } from '../lib/labels'

// The 10 core sections read the environment on its own terms; the numbering is the
// contract's own order, and the connection section closes it as 11.
const coreSectionTitles: Record<string, string> = {
  regime_summary: '1. レジーム要約',
  rates_policy: '2. 金利・金融政策',
  growth_demand: '3. 景気・需要',
  inflation_costs: '4. インフレ・コスト',
  liquidity_credit: '5. 流動性・信用・リスク選好',
  fx: '6. 為替',
  japan: '7. 日本',
  valuation: '8. バリュエーション',
  risk_environment: '9. リスク選好環境の評価とシナリオ',
  monitoring: '10. 監視ポイント',
}

const connectionSectionTitle = '11. 日本株積立ループ接続'

const comparisonSymbols: Record<string, string> = {
  below: '<',
  at_or_below: '≤',
  above: '>',
  at_or_above: '≥',
}

function SourceIds({ ids }: { ids: readonly string[] }) {
  if (ids.length === 0) return null
  return <p className="mt-1 font-mono text-[10px] leading-relaxed text-muted-foreground/75">出典: {ids.join(', ')}</p>
}

function SectionShell({ title, seriesBadges, children }: { title: string; seriesBadges: readonly MacroSeriesReferenceView[]; children: React.ReactNode }) {
  return (
    <SectionCard
      meta={(
        <div className="flex flex-wrap justify-end gap-2">
          {seriesBadges.map((series) => <Badge key={series.series_id} variant="outline">{series.name} · {series.series_id}</Badge>)}
        </div>
      )}
      padded
      title={title}
    >
      <div className="grid gap-5 lg:grid-cols-3">{children}</div>
    </SectionCard>
  )
}

// The channel titles without their reading-order numbers, for naming which channels a
// dominant force crosses.
const channelShortTitles: Record<string, string> = {
  rates_policy: '金利・金融政策',
  growth_demand: '景気・需要',
  inflation_costs: 'インフレ・コスト',
  liquidity_credit: '流動性・信用',
  fx: '為替',
  japan: '日本',
  valuation: 'バリュエーション',
}

function SynthesisSection({ synthesis }: { synthesis: MacroSynthesisView }) {
  const forces = synthesis.dominant_forces ?? []
  const interactions = synthesis.interactions ?? []
  return (
    <SectionCard description="複数の伝達チャネルを横断して現局面を動かしている力と、その相互作用。" padded title="統合評価 — 支配的な力">
      <div className="grid gap-4">
        <div className="grid gap-4 lg:grid-cols-2">
          {forces.map((force) => (
            <div className="grid content-start gap-2 rounded-lg border p-4" key={force.force_id}>
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="text-sm font-semibold">{force.title}</h3>
                <Badge variant="secondary">{force.direction} / {force.confidence}</Badge>
              </div>
              <div className="flex flex-wrap gap-1.5">
                {force.core_section_ids.map((id) => <Badge key={id} variant="outline">{channelShortTitles[id] ?? id}</Badge>)}
              </div>
              <p className="text-sm">{force.summary}</p>
              <p className="text-sm text-muted-foreground"><span className="font-medium text-foreground">伝達経路:</span> {force.transmission}</p>
              <p className="text-sm text-muted-foreground"><span className="font-medium text-foreground">反証:</span> {force.counter_evidence}</p>
              <div className="flex flex-wrap gap-1.5">
                {force.series.map((series) => <Badge key={series.series_id} variant="outline">{series.name} · {series.series_id}</Badge>)}
              </div>
              <SourceIds ids={force.source_ids} />
            </div>
          ))}
        </div>
        {interactions.length > 0 && (
          <div className="grid gap-3">
            {interactions.map((interaction, index) => (
              <Alert key={`interaction-${index}`} role="note">
                <CircleAlert />
                <AlertTitle>相互作用 · {interaction.force_ids.join(' × ')}</AlertTitle>
                <AlertDescription>{interaction.summary}<SourceIds ids={interaction.source_ids} /></AlertDescription>
              </Alert>
            ))}
          </div>
        )}
      </div>
    </SectionCard>
  )
}

function ScenarioCard({ scenario }: { scenario: MacroScenarioView }) {
  return (
    <div className="grid content-start gap-2 rounded-lg border p-4">
      <div className="flex items-center gap-2">
        <h4 className="font-semibold uppercase">{scenario.case}</h4>
        {scenario.probability != null && <Badge>{Math.round(scenario.probability * 100)}%</Badge>}
        <Badge variant="secondary">{scenario.direction}</Badge>
      </div>
      <p className="text-sm">{scenario.summary}</p>
      {scenario.conditions.length > 0 && <div><p className="text-xs font-medium">成立条件</p>{scenario.conditions.map((condition, index) => <p className="text-xs text-muted-foreground" key={`condition-${index}`}>{condition}</p>)}</div>}
      {scenario.scorecard.length > 0 && (
        <div>
          <p className="text-xs font-medium">機械照合条件（scorecard）</p>
          {scenario.scorecard.map((condition, index) => (
            <p className="font-mono text-[11px] text-muted-foreground tabular-nums" key={`${condition.series_id}-${index}`}>
              {condition.series_id} {comparisonSymbols[condition.comparison] ?? condition.comparison} {condition.threshold} · 期限 {condition.deadline}
            </p>
          ))}
        </div>
      )}
      {scenario.economic_implications.length > 0 && <div><p className="text-xs font-medium">経済経路への含意</p>{scenario.economic_implications.map((item, index) => <p className="text-xs text-muted-foreground" key={`implication-${index}`}>{item}</p>)}</div>}
      <SourceIds ids={scenario.source_ids} />
    </div>
  )
}

function CoreSection({ section }: { section: MacroCoreSectionView }) {
  const riskEnvironment = section.risk_environment
  return (
    <SectionShell seriesBadges={section.series} title={coreSectionTitles[section.section_id] ?? section.section_id}>
      <div className="grid content-start gap-2 rounded-lg bg-muted/45 p-4">
        <h3 className="text-sm font-semibold">Fact 要約</h3>
        {section.fact_summary.map((fact, index) => <div key={index}><p className="text-sm">{fact.summary}</p><SourceIds ids={fact.source_ids} /></div>)}
        {section.change_since_previous && <p className="border-t pt-2 text-sm"><span className="font-medium">前回からの変化:</span> {section.change_since_previous}</p>}
        {section.previous_scorecard_review && <p className="border-t pt-2 text-sm"><span className="font-medium">前回 scorecard の採点:</span> {section.previous_scorecard_review}</p>}
      </div>
      <div className="grid content-start gap-2 rounded-lg border p-4">
        <div className="flex flex-wrap items-center gap-2"><h3 className="text-sm font-semibold">Judgment</h3><Badge variant="secondary">{section.judgment.direction} / {section.judgment.confidence}</Badge></div>
        <p className="text-sm">{section.judgment.summary}</p>
        <SourceIds ids={section.judgment.source_ids} />
        {section.material_deltas.map((delta, index) => <div className="mt-2 border-t pt-3" key={`${delta.channel}-${index}`}><div className="mb-1 flex flex-wrap gap-2"><Badge variant="outline">{delta.channel}</Badge><Badge variant="secondary">{delta.direction} / {delta.materiality}</Badge></div><p className="text-sm">{delta.summary}</p><p className="mt-1 text-xs text-muted-foreground">{delta.used_for}</p><SourceIds ids={delta.source_ids} /></div>)}
      </div>
      <div className="grid content-start gap-2 rounded-lg border p-4">
        <h3 className="text-sm font-semibold">経済経路への接続</h3>
        <p className="text-sm">{section.economic_connection.summary}</p>
        <SourceIds ids={section.economic_connection.source_ids} />
      </div>
      {riskEnvironment && (
        <div className="grid content-start gap-2 rounded-lg border p-4 lg:col-span-3">
          <div className="flex flex-wrap items-center gap-2"><h3 className="text-sm font-semibold">リスク選好環境</h3><Badge variant="secondary">{riskEnvironment.stance} / {riskEnvironment.confidence}</Badge></div>
          <p className="text-sm">{riskEnvironment.summary}</p>
          <div><p className="text-xs font-medium">反証条件</p>{riskEnvironment.falsifiers.map((falsifier, index) => <p className="text-xs text-muted-foreground" key={`falsifier-${index}`}>{falsifier}</p>)}</div>
          <SourceIds ids={riskEnvironment.source_ids} />
        </div>
      )}
      {section.scenarios.length > 0 && <div className="grid gap-3 lg:col-span-3 lg:grid-cols-3">{section.scenarios.map((scenario) => <ScenarioCard key={scenario.case} scenario={scenario} />)}</div>}
      {section.monitoring_points.length > 0 && <div className="grid gap-3 lg:col-span-3">{section.monitoring_points.map((point, index) => <Alert key={index} role="note"><CircleAlert /><AlertTitle>{point.event}</AlertTitle><AlertDescription>{point.summary}<br />条件: {point.condition}<br />見方の変更: {point.view_change}<SourceIds ids={point.source_ids} /></AlertDescription></Alert>)}</div>}
    </SectionShell>
  )
}

function ConnectionSection({ section }: { section: MacroConnectionSectionView }) {
  return (
    <SectionShell seriesBadges={section.series} title={connectionSectionTitle}>
      <div className="grid content-start gap-2 rounded-lg bg-muted/45 p-4">
        <h3 className="text-sm font-semibold">Fact 要約</h3>
        {section.fact_summary.map((fact, index) => <div key={index}><p className="text-sm">{fact.summary}</p><SourceIds ids={fact.source_ids} /></div>)}
        <p className="border-t pt-2 text-sm"><span className="font-medium">依拠する core セクション:</span> {section.core_section_ids.map((id) => coreSectionTitles[id] ?? id).join(' / ')}</p>
      </div>
      <div className="grid content-start gap-2 rounded-lg border p-4">
        <div className="flex flex-wrap items-center gap-2"><h3 className="text-sm font-semibold">Judgment</h3><Badge variant="secondary">{section.judgment.direction} / {section.judgment.confidence}</Badge></div>
        <p className="text-sm">{section.judgment.summary}</p>
        <SourceIds ids={section.judgment.source_ids} />
      </div>
      <div className="grid content-start gap-3 rounded-lg border p-4">
        <h3 className="text-sm font-semibold">Research 優先度ヒント</h3>
        {/* applies_to is what gives a hint its discriminating power, so it is shown with every
            hint rather than folded into the summary. */}
        {section.research_priority_hints.map((hint, index) => <div key={`hint-${index}`}><Badge variant="outline">効く候補: {hint.applies_to}</Badge><p className="mt-1 text-sm">{hint.summary}</p><SourceIds ids={hint.source_ids} /></div>)}
      </div>
      {section.bargain_topography && (
        <div className="grid content-start gap-2 rounded-lg border p-4 lg:col-span-3">
          <h3 className="text-sm font-semibold">バーゲン地形</h3>
          <p className="text-sm">{section.bargain_topography.summary}</p>
          <SourceIds ids={section.bargain_topography.source_ids} />
        </div>
      )}
      {(section.estimate_caveats ?? []).length > 0 && (
        <div className="grid content-start gap-3 rounded-lg border p-4 lg:col-span-3">
          <h3 className="text-sm font-semibold">機械見積りへの注意</h3>
          <div className="grid gap-3 lg:grid-cols-2">
            {(section.estimate_caveats ?? []).map((caveat, index) => (
              <div key={`caveat-${index}`}>
                <div className="flex flex-wrap gap-2">
                  <Badge variant="outline">{caveat.affected_component}</Badge>
                  <Badge variant="secondary">{caveat.materiality}</Badge>
                  <Badge variant="outline">効く候補: {caveat.applies_to}</Badge>
                </div>
                <p className="mt-1 text-sm">{caveat.summary}</p>
                <SourceIds ids={caveat.source_ids} />
              </div>
            ))}
          </div>
        </div>
      )}
      {section.sector_tilts.length > 0 && (
        <div className="grid content-start gap-3 rounded-lg border p-4 lg:col-span-3">
          <h3 className="text-sm font-semibold">Sector tilt</h3>
          <div className="grid gap-3 lg:grid-cols-3">
            {section.sector_tilts.map((tilt, index) => <div key={`tilt-${index}`}><div className="flex flex-wrap gap-2"><Badge variant="outline">{tilt.sector}</Badge><Badge variant="secondary">{tilt.direction}</Badge></div><p className="mt-1 text-sm">{tilt.summary}</p><SourceIds ids={tilt.source_ids} /></div>)}
          </div>
        </div>
      )}
      {section.sizing_cautions.length > 0 && <div className="grid gap-3 lg:col-span-3">{section.sizing_cautions.map((caution, index) => <Alert key={`caution-${index}`} role="note"><CircleAlert /><AlertTitle>Sizing caution · {caution.severity}</AlertTitle><AlertDescription>{caution.summary}<SourceIds ids={caution.source_ids} /></AlertDescription></Alert>)}</div>}
    </SectionShell>
  )
}

export function MacroReportPage() {
  const { contextId = '' } = useParams<{ contextId: string }>()
  const [data, setData] = useState<MacroContextView | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setError(null)
    setData(null)
    fetchJson<MacroContextView>(`/api/macro/context/${encodeURIComponent(contextId)}`)
      .then(setData)
      .catch((reason: unknown) => {
        setError(reason instanceof Error ? reason.message : 'レポートを読み込めませんでした')
      })
  }, [contextId])

  if (error) return <PageState message={error} title="Macro report" />
  if (!data) return <LoadingPage label="レポートを読み込んでいます" />

  // Degrade gracefully rather than white-screen if a served view is ever missing a
  // section (e.g. a stale view during a deploy that precedes its re-materialization).
  const core = data.core ?? []
  const connection = data.connection

  return (
    // The page is named for what it is; the summary is data and stays in the card, where
    // a long sentence reads as a sentence rather than as a heading.
    <PageShell
      above={<div><Button asChild size="sm" variant="ghost"><Link to="/macro"><ArrowLeft />Macro に戻る</Link></Button></div>}
      meta={data.stale ? <StaleBadge /> : undefined}
      title="マクロ環境レポート"
      width="reading"
    >
      <Card className="shadow-sm">
        <CardHeader>
          <CardTitle>{data.summary}</CardTitle>
          <CardDescription>{data.context_id} · {LABEL.asOf} {data.as_of}（{data.age_days} 日前） · {LABEL.published} {formatJstDateTime(data.published_at)}</CardDescription>
        </CardHeader>
      </Card>
      {core.length === 0 && <Alert><CircleAlert /><AlertTitle>セクションを表示できません</AlertTitle><AlertDescription>この revision の core セクションが served view に含まれていません。view の再生成待ちの可能性があります。</AlertDescription></Alert>}
      {data.synthesis && <SynthesisSection synthesis={data.synthesis} />}
      {core.map((section) => <CoreSection key={section.section_id} section={section} />)}
      {connection && <ConnectionSection section={connection} />}
    </PageShell>
  )
}
