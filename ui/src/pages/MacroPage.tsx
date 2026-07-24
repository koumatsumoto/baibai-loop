import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowDown, ArrowRight, ArrowUp, CircleAlert, Minus } from 'lucide-react'
import { CartesianGrid, Line, LineChart, ResponsiveContainer, XAxis, YAxis } from 'recharts'

import { fetchJson } from '../api/client'
import type { MacroSeriesView, MacroView } from '../api/types'
import { AppShell } from '../components/AppShell'
import { LoadingIndicator, LoadingPage } from '../components/LoadingIndicator'
import { PageState } from '../components/PageState'
import { StaleBadge } from '../components/StaleBadge'
import { TradingViewButton } from '../components/TradingViewButton'
import { Alert, AlertDescription, AlertTitle } from '../components/ui/alert'
import { Card, CardContent } from '../components/ui/card'
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from '../components/ui/chart'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../components/ui/select'
import { formatJstDateTime, formatNumber } from '../lib/format'
import { LABEL } from '../lib/labels'
import { seriesWindowSummary } from '../lib/macro'
import { cn } from '../lib/utils'

type MacroPeriod = MacroView['period']
type MacroGranularity = MacroView['granularity']

function fmtValue(value: number): string {
  const magnitude = Math.abs(value)
  const digits = magnitude >= 100 ? 0 : magnitude >= 1 ? 2 : 3
  return formatNumber(value, digits)
}

function DeltaBadge({ delta }: { delta: number | null }) {
  if (delta === null) return null
  // Neutral direction only: macro series have no universal good/bad sign.
  const Icon = delta > 0 ? ArrowUp : delta < 0 ? ArrowDown : Minus
  return (
    <span className="inline-flex items-center gap-0.5 text-xs text-muted-foreground tabular-nums">
      <Icon aria-hidden="true" className="size-3" />{fmtValue(Math.abs(delta))}
    </span>
  )
}

function Sparkline({ points }: { points: MacroSeriesView['points'] }) {
  if (points.length === 0) return <div className="h-10" aria-hidden="true" />
  return (
    <div className="h-10 w-full">
      <ResponsiveContainer height="100%" width="100%">
        <LineChart data={points} margin={{ top: 2, right: 2, bottom: 2, left: 2 }}>
          <YAxis domain={['auto', 'auto']} hide />
          <Line dataKey="value" dot={false} isAnimationActive={false} stroke="var(--chart-1)" strokeWidth={1.5} type="monotone" />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

function FullChart({ series }: { series: MacroSeriesView }) {
  const config = { value: { label: series.label, color: 'var(--chart-1)' } } satisfies ChartConfig
  if (series.points.length === 0) return <p className="py-12 text-center text-sm text-muted-foreground">観測値なし</p>
  return (
    <ChartContainer className="h-56 w-full" config={config}>
      <LineChart data={series.points} margin={{ left: 4, right: 12 }}>
        <CartesianGrid vertical={false} />
        <XAxis axisLine={false} dataKey="observed_at" minTickGap={28} tickLine={false} />
        <YAxis axisLine={false} domain={['auto', 'auto']} tickLine={false} width={52} />
        <ChartTooltip content={<ChartTooltipContent />} />
        <Line dataKey="value" dot={false} stroke="var(--color-value)" strokeWidth={2} type="monotone" />
      </LineChart>
    </ChartContainer>
  )
}

function SeriesCard({ series, expanded, onToggle }: { series: MacroSeriesView; expanded: boolean; onToggle: () => void }) {
  const summary = seriesWindowSummary(series.points)
  return (
    <Card className={cn('overflow-hidden py-0 shadow-sm', expanded && 'col-span-full')}>
      <button
        aria-expanded={expanded}
        className="flex w-full items-start justify-between gap-2 px-4 py-3 text-left transition-colors hover:bg-muted/40"
        onClick={onToggle}
        type="button"
      >
        <div className="min-w-0">
          <p className="truncate text-sm font-medium" title={series.label}>{series.label}</p>
          <p className="font-mono text-[10px] text-muted-foreground">{series.series_id} · {series.unit}</p>
        </div>
        <div className="flex shrink-0 flex-col items-end">
          <span className="font-mono text-sm font-semibold tabular-nums">{summary.latest === null ? '—' : fmtValue(summary.latest)}</span>
          <DeltaBadge delta={summary.delta} />
        </div>
      </button>
      {expanded ? (
        <CardContent className="grid gap-2 px-4 pb-4">
          {series.tradingview_symbol && <div className="flex justify-end"><TradingViewButton name={series.label} symbol={series.tradingview_symbol} /></div>}
          <FullChart series={series} />
        </CardContent>
      ) : (
        <div className="px-2 pb-2"><Sparkline points={series.points} /></div>
      )}
    </Card>
  )
}

export function MacroPage() {
  const [data, setData] = useState<MacroView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [period, setPeriod] = useState<MacroPeriod>('max')
  const [granularity, setGranularity] = useState<MacroGranularity>('monthly')
  const [loading, setLoading] = useState(true)
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set())

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
  if (!data) return <LoadingPage label="Macro を読み込んでいます" />

  // Degrade gracefully rather than white-screen if a served view is ever missing a
  // field (e.g. a stale view during a deploy that precedes its re-materialization).
  const reports = data.reports ?? []
  const groups = data.groups ?? []

  const toggle = (seriesId: string) => {
    setExpanded((current) => {
      const next = new Set(current)
      if (next.has(seriesId)) next.delete(seriesId)
      else next.add(seriesId)
      return next
    })
  }

  return (
    <><AppShell /><main className="mx-auto grid max-w-[1600px] gap-8 px-4 py-6 sm:px-6 lg:px-8">
      <section className="grid gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">経済分析レポート</h1>
        {reports.length === 0
          ? <Alert><CircleAlert /><AlertTitle>経済分析レポートなし</AlertTitle><AlertDescription>マクロ経済指標は下段で確認できます。分析レポートは publish 後に表示されます。</AlertDescription></Alert>
          : (
            <Card className="divide-y py-0 shadow-sm">
              {reports.map((report) => (
                <Link className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 px-5 py-3 transition-colors hover:bg-muted/40" key={report.context_id} to={`/macro/reports/${report.context_id}`}>
                  <div className="flex min-w-0 items-center gap-2">
                    <span className="truncate text-sm font-medium">{report.summary}</span>
                    {report.stale && <StaleBadge />}
                  </div>
                  <div className="flex shrink-0 items-center gap-3 font-mono text-xs text-muted-foreground tabular-nums">
                    <span>{LABEL.asOf} {report.as_of}</span>
                    <span className="hidden sm:inline">{LABEL.published} {formatJstDateTime(report.published_at)}</span>
                    <ArrowRight aria-hidden="true" className="size-4" />
                  </div>
                </Link>
              ))}
            </Card>
          )}
      </section>

      <section className="grid gap-5">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div><h2 className="text-2xl font-semibold tracking-tight">マクロ経済指標</h2><p className="mt-1 text-sm text-muted-foreground">カードをクリックでチャート展開</p></div>
          <div className="flex flex-wrap gap-3 rounded-xl border bg-card p-3 shadow-sm">
            <label className="grid gap-1"><span className="text-xs font-medium text-muted-foreground">期間</span><Select onValueChange={(value) => setPeriod(value as MacroPeriod)} value={period}><SelectTrigger aria-label="表示期間" className="w-24"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="1y">1年</SelectItem><SelectItem value="5y">5年</SelectItem><SelectItem value="10y">10年</SelectItem><SelectItem value="max">全期間</SelectItem></SelectContent></Select></label>
            <label className="grid gap-1"><span className="text-xs font-medium text-muted-foreground">粒度</span><Select onValueChange={(value) => setGranularity(value as MacroGranularity)} value={granularity}><SelectTrigger aria-label="表示粒度" className="w-28"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="daily">日次</SelectItem><SelectItem value="weekly">週次</SelectItem><SelectItem value="monthly">月次</SelectItem><SelectItem value="yearly">年次</SelectItem></SelectContent></Select></label>
            {loading && <LoadingIndicator className="self-end pb-2" label="マクロ経済指標を更新しています" size={24} />}
          </div>
        </div>
        {groups.map((group) => (
          <div className="grid gap-3" key={group.title}>
            <h3 className="text-lg font-semibold">{group.title}</h3>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
              {group.series.map((series) => <SeriesCard expanded={expanded.has(series.series_id)} key={series.series_id} onToggle={() => toggle(series.series_id)} series={series} />)}
            </div>
          </div>
        ))}
      </section>
    </main></>
  )
}
