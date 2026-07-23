import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ArrowLeft, CircleAlert } from 'lucide-react'

import { fetchJson } from '../api/client'
import type { MacroContextSectionView, MacroContextView } from '../api/types'
import { AppShell } from '../components/AppShell'
import { PageState } from '../components/PageState'
import { StaleBadge } from '../components/StaleBadge'
import { Alert, AlertDescription, AlertTitle } from '../components/ui/alert'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/card'
import { formatJstDateTime } from '../lib/format'
import { LABEL } from '../lib/labels'

const sectionTitles: Record<string, string> = {
  regime_summary: '1. Regime summary',
  rates_policy: '2. 金利・金融政策',
  growth_demand: '3. 景気・需要',
  inflation_costs: '4. インフレ・コスト',
  fx_liquidity: '5. 為替・流動性',
  japan_specific: '6. 日本固有',
  scenarios_connections: '7. シナリオと接続',
  monitoring_points: '8. 監視ポイント',
}

function SourceIds({ ids }: { ids: readonly string[] }) {
  if (ids.length === 0) return null
  return <p className="mt-1 font-mono text-[10px] leading-relaxed text-muted-foreground/75">出典: {ids.join(', ')}</p>
}

function ReportSection({ section }: { section: MacroContextSectionView }) {
  return (
    <Card className="gap-4 py-5 shadow-sm">
      <CardHeader className="gap-3 px-5">
        <CardTitle aria-level={2} className="text-lg" role="heading">{sectionTitles[section.section_id] ?? section.section_id}</CardTitle>
        <div className="flex flex-wrap gap-2">
          {section.series.map((series) => <Badge key={series.series_id} variant="outline">{series.name} · {series.series_id}</Badge>)}
        </div>
      </CardHeader>
      <CardContent className="grid gap-5 px-5 lg:grid-cols-3">
        <div className="grid content-start gap-2 rounded-lg bg-muted/45 p-4">
          <h3 className="text-sm font-semibold">Fact 要約</h3>
          {section.fact_summary.map((fact, index) => <div key={index}><p className="text-sm">{fact.summary}</p><SourceIds ids={fact.source_ids} /></div>)}
          {section.change_since_previous && <p className="border-t pt-2 text-sm"><span className="font-medium">比較:</span> {section.change_since_previous}</p>}
        </div>
        <div className="grid content-start gap-2 rounded-lg border p-4">
          <div className="flex flex-wrap items-center gap-2"><h3 className="text-sm font-semibold">Judgment</h3><Badge variant="secondary">{section.judgment.direction} / {section.judgment.confidence}</Badge></div>
          <p className="text-sm">{section.judgment.summary}</p>
          <SourceIds ids={section.judgment.source_ids} />
          {section.material_deltas.map((delta, index) => <div className="mt-2 border-t pt-3" key={`${delta.channel}-${index}`}><div className="mb-1 flex flex-wrap gap-2"><Badge variant="outline">{delta.channel}</Badge><Badge variant="secondary">{delta.direction} / {delta.materiality}</Badge></div><p className="text-sm">{delta.summary}</p><p className="mt-1 text-xs text-muted-foreground">{delta.used_for}</p><SourceIds ids={delta.source_ids} /></div>)}
          {section.sizing_cautions.map((caution, index) => <Alert className="mt-2" key={index} role="note"><CircleAlert /><AlertTitle>Sizing caution · {caution.severity}</AlertTitle><AlertDescription>{caution.summary}<SourceIds ids={caution.source_ids} /></AlertDescription></Alert>)}
        </div>
        <div className="grid content-start gap-2 rounded-lg border p-4">
          <h3 className="text-sm font-semibold">投資判断への接続</h3>
          <p className="text-sm">{section.investment_connection.summary}</p>
          {section.investment_connection.sector_tilts.map((item, index) => <p className="text-sm" key={`tilt-${index}`}><span className="font-medium">Sector tilt:</span> {item}</p>)}
          {section.investment_connection.research_priority_hints.map((item, index) => <p className="text-sm" key={`priority-${index}`}><span className="font-medium">Research priority:</span> {item}</p>)}
          <SourceIds ids={section.investment_connection.source_ids} />
        </div>
        {section.scenarios.length > 0 && <div className="grid gap-3 lg:col-span-3 lg:grid-cols-3">{section.scenarios.map((scenario) => <div className="rounded-lg border p-4" key={scenario.case}><div className="mb-2 flex items-center gap-2"><h3 className="font-semibold uppercase">{scenario.case}</h3><Badge variant="secondary">{scenario.direction}</Badge></div><p className="text-sm">{scenario.summary}</p><p className="mt-2 text-xs text-muted-foreground">条件: {scenario.conditions.join(' / ')}</p><p className="mt-1 text-xs text-muted-foreground">接続: {scenario.investment_implications.join(' / ')}</p><SourceIds ids={scenario.source_ids} /></div>)}</div>}
        {section.monitoring_points.length > 0 && <div className="grid gap-3 lg:col-span-3">{section.monitoring_points.map((point, index) => <Alert key={index} role="note"><CircleAlert /><AlertTitle>{point.event}</AlertTitle><AlertDescription>{point.summary}<br />条件: {point.condition}<br />見方の変更: {point.view_change}<SourceIds ids={point.source_ids} /></AlertDescription></Alert>)}</div>}
      </CardContent>
    </Card>
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
  if (!data) return <PageState message="レポートを読み込んでいます…" title="Macro report" />

  return (
    <><AppShell /><main className="mx-auto grid max-w-[1600px] gap-6 px-4 py-6 sm:px-6 lg:px-8">
      <div>
        <Button asChild size="sm" variant="ghost"><Link to="/macro"><ArrowLeft />Macro に戻る</Link></Button>
      </div>
      <Card className="shadow-sm">
        <CardHeader className="border-b">
          <div className="flex flex-wrap items-center gap-2"><CardTitle>{data.summary}</CardTitle>{data.stale && <StaleBadge />}</div>
          <CardDescription>{data.context_id} · {LABEL.asOf} {data.as_of} · {LABEL.published} {formatJstDateTime(data.published_at)} · valid until {data.valid_until}</CardDescription>
        </CardHeader>
      </Card>
      {data.sections.length === 0
        ? <Alert><CircleAlert /><AlertTitle>Summary 表示</AlertTitle><AlertDescription>この revision は共通 field のみを持ちます。</AlertDescription></Alert>
        : data.sections.map((section) => <ReportSection key={section.section_id} section={section} />)}
    </main></>
  )
}
