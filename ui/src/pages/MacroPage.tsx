import { useEffect, useState } from 'react'
import { ChartNoAxesCombined, CircleAlert } from 'lucide-react'
import { CartesianGrid, Line, LineChart, XAxis, YAxis } from 'recharts'

import { fetchJson } from '../api/client'
import type { MacroContextSectionView, MacroSeriesView, MacroView } from '../api/types'
import { AppShell } from '../components/AppShell'
import { Alert, AlertDescription, AlertTitle } from '../components/ui/alert'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/card'
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from '../components/ui/chart'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../components/ui/select'
import { Tooltip, TooltipContent, TooltipTrigger } from '../components/ui/tooltip'
import { formatJstDateTime } from '../lib/utils'
import { tradingViewSymbolChartUrl } from '../lib/trading-view'

type MacroPeriod = MacroView['period']
type MacroGranularity = MacroView['granularity']

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

function PageState({ message }: { message: string }) {
  return <><AppShell /><main className="grid min-h-[60vh] place-items-center px-6 text-center"><h1 className="text-xl font-semibold">{message}</h1></main></>
}

function SeriesChart({ series }: { series: MacroSeriesView }) {
  const config = { value: { label: series.label, color: 'var(--chart-1)' } } satisfies ChartConfig
  return (
    <Card className="gap-3 py-5 shadow-sm">
      <CardHeader className="px-5">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="text-base">{series.label}</CardTitle>
          {series.tradingview_symbol && <Tooltip><TooltipTrigger asChild><Button asChild size="icon-sm" variant="ghost"><a aria-label={`${series.label} の TradingView チャートを開く`} href={tradingViewSymbolChartUrl(series.tradingview_symbol)} rel="noopener noreferrer" target="_blank"><ChartNoAxesCombined aria-hidden="true" /></a></Button></TooltipTrigger><TooltipContent>TradingView でチャートを開く</TooltipContent></Tooltip>}
        </div>
        <CardDescription>{series.series_id} · {series.unit}</CardDescription>
      </CardHeader>
      <CardContent className="px-3 sm:px-5">
        {series.points.length === 0 ? <p className="py-12 text-center text-sm text-muted-foreground">観測値なし</p> : (
          <ChartContainer className="h-52 w-full" config={config}>
            <LineChart data={series.points} margin={{ left: 4, right: 12 }}>
              <CartesianGrid vertical={false} />
              <XAxis dataKey="observed_at" minTickGap={28} tickLine={false} axisLine={false} />
              <YAxis domain={['auto', 'auto']} width={44} tickLine={false} axisLine={false} />
              <ChartTooltip content={<ChartTooltipContent />} />
              <Line dataKey="value" type="monotone" stroke="var(--color-value)" strokeWidth={2} dot={false} />
            </LineChart>
          </ChartContainer>
        )}
      </CardContent>
    </Card>
  )
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
          {section.fact_summary.map((fact, index) => <p className="text-sm" key={index}>{fact.summary}</p>)}
          {section.change_since_previous && <p className="border-t pt-2 text-sm"><span className="font-medium">比較:</span> {section.change_since_previous}</p>}
        </div>
        <div className="grid content-start gap-2 rounded-lg border p-4">
          <div className="flex flex-wrap items-center gap-2"><h3 className="text-sm font-semibold">Judgment</h3><Badge variant="secondary">{section.judgment.direction} / {section.judgment.confidence}</Badge></div>
          <p className="text-sm">{section.judgment.summary}</p>
          {section.material_deltas.map((delta, index) => <div className="mt-2 border-t pt-3" key={`${delta.channel}-${index}`}><div className="mb-1 flex flex-wrap gap-2"><Badge variant="outline">{delta.channel}</Badge><Badge variant="secondary">{delta.direction} / {delta.materiality}</Badge></div><p className="text-sm">{delta.summary}</p><p className="mt-1 text-xs text-muted-foreground">{delta.used_for}</p></div>)}
          {section.sizing_cautions.map((caution, index) => <Alert className="mt-2" key={index} role="note"><CircleAlert /><AlertTitle>Sizing caution · {caution.severity}</AlertTitle><AlertDescription>{caution.summary}</AlertDescription></Alert>)}
        </div>
        <div className="grid content-start gap-2 rounded-lg border p-4">
          <h3 className="text-sm font-semibold">投資判断への接続</h3>
          <p className="text-sm">{section.investment_connection.summary}</p>
          {section.investment_connection.sector_tilts.map((item, index) => <p className="text-sm" key={`tilt-${index}`}><span className="font-medium">Sector tilt:</span> {item}</p>)}
          {section.investment_connection.research_priority_hints.map((item, index) => <p className="text-sm" key={`priority-${index}`}><span className="font-medium">Research priority:</span> {item}</p>)}
        </div>
        {section.scenarios.length > 0 && <div className="grid gap-3 lg:col-span-3 lg:grid-cols-3">{section.scenarios.map((scenario) => <div className="rounded-lg border p-4" key={scenario.case}><div className="mb-2 flex items-center gap-2"><h3 className="font-semibold uppercase">{scenario.case}</h3><Badge variant="secondary">{scenario.direction}</Badge></div><p className="text-sm">{scenario.summary}</p><p className="mt-2 text-xs text-muted-foreground">条件: {scenario.conditions.join(' / ')}</p><p className="mt-1 text-xs text-muted-foreground">接続: {scenario.investment_implications.join(' / ')}</p></div>)}</div>}
        {section.monitoring_points.length > 0 && <div className="grid gap-3 lg:col-span-3">{section.monitoring_points.map((point, index) => <Alert key={index} role="note"><CircleAlert /><AlertTitle>{point.event}</AlertTitle><AlertDescription>{point.summary}<br />条件: {point.condition}<br />見方の変更: {point.view_change}</AlertDescription></Alert>)}</div>}
      </CardContent>
    </Card>
  )
}

export function MacroPage() {
  const [data, setData] = useState<MacroView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [period, setPeriod] = useState<MacroPeriod>('1y')
  const [granularity, setGranularity] = useState<MacroGranularity>('daily')
  const [loading, setLoading] = useState(true)
  useEffect(() => {
    const controller = new AbortController()
    setError(null)
    setLoading(true)
    const query = new URLSearchParams({ period, granularity })
    fetchJson<MacroView>(`/api/macro?${query}`, { signal: controller.signal })
      .then(setData)
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === 'AbortError') return
        setError(reason instanceof Error ? reason.message : 'Macro を読み込めませんでした')
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [period, granularity])
  if (error) return <PageState message={error} />
  if (!data) return <PageState message="Macro を読み込んでいます…" />
  const context = data.context
  return (
    <><AppShell /><main className="mx-auto grid max-w-[1600px] gap-8 px-4 py-6 sm:px-6 lg:px-8">
      <section className="grid gap-4">
        <div><p className="text-sm font-medium text-muted-foreground">Judgment</p><h1 className="text-2xl font-semibold tracking-tight">Macro context</h1></div>
        {!context ? <Alert><CircleAlert /><AlertTitle>Published context なし</AlertTitle><AlertDescription>指標は fact として表示します。投資判断用 context は publish 後に現れます。</AlertDescription></Alert> : <>
          <Card className="shadow-sm">
            <CardHeader className="border-b">
              <div className="flex flex-wrap items-center gap-2"><CardTitle>{context.summary}</CardTitle>{context.stale && <Badge variant="destructive">STALE</Badge>}</div>
              <CardDescription>{context.context_id} · 基準 (as-of) {context.as_of} · 公表 {formatJstDateTime(context.published_at)} · valid until {context.valid_until}</CardDescription>
            </CardHeader>
          </Card>
          {context.sections.length === 0 ? <Alert><CircleAlert /><AlertTitle>Summary 表示</AlertTitle><AlertDescription>この revision は共通 field のみを表示します。</AlertDescription></Alert> : context.sections.map((section) => <ReportSection key={section.section_id} section={section} />)}
        </>}
        {data.context_history.length > 0 && <Card className="gap-3 py-5 shadow-sm"><CardHeader className="px-5"><CardTitle className="text-base">Published history</CardTitle><CardDescription>immutable revisions</CardDescription></CardHeader><CardContent className="grid gap-2 px-5">{data.context_history.map((revision) => <div className="flex flex-wrap items-baseline justify-between gap-2 border-b py-2 last:border-0" key={revision.context_id}><div><p className="text-sm font-medium">{revision.summary}</p><p className="font-mono text-xs text-muted-foreground">{revision.context_id}</p></div><div className="text-right font-mono text-xs text-muted-foreground tabular-nums"><div>基準 {revision.as_of}</div><div>公表 {formatJstDateTime(revision.published_at)}</div></div></div>)}</CardContent></Card>}
      </section>
      <section className="grid gap-5">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div><p className="text-sm font-medium text-muted-foreground">Fact</p><h2 className="text-2xl font-semibold tracking-tight">Macro indicators</h2></div>
          <div className="flex flex-wrap gap-3 rounded-xl border bg-card p-3 shadow-sm">
            <label className="grid gap-1"><span className="text-xs font-medium text-muted-foreground">期間</span><Select onValueChange={(value) => setPeriod(value as MacroPeriod)} value={period}><SelectTrigger aria-label="表示期間" className="w-24"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="1y">1年</SelectItem><SelectItem value="5y">5年</SelectItem><SelectItem value="10y">10年</SelectItem><SelectItem value="max">全期間</SelectItem></SelectContent></Select></label>
            <label className="grid gap-1"><span className="text-xs font-medium text-muted-foreground">粒度</span><Select onValueChange={(value) => setGranularity(value as MacroGranularity)} value={granularity}><SelectTrigger aria-label="表示粒度" className="w-28"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="daily">日次</SelectItem><SelectItem value="weekly">週次</SelectItem><SelectItem value="monthly">月次</SelectItem><SelectItem value="yearly">年次</SelectItem></SelectContent></Select></label>
            {loading && <span className="self-end pb-2 text-xs text-muted-foreground">更新中…</span>}
          </div>
        </div>
        {data.groups.map((group) => <div className="grid gap-4" key={group.title}><h3 className="text-lg font-semibold">{group.title}</h3><div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">{group.series.map((series) => <SeriesChart key={series.series_id} series={series} />)}</div></div>)}
      </section>
    </main></>
  )
}
