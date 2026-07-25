import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowDown, ArrowRight, ArrowUp, CircleAlert, Minus } from 'lucide-react'
import { CartesianGrid, Line, LineChart, ResponsiveContainer, XAxis, YAxis } from 'recharts'

import { fetchJson } from '../api/client'
import type { MacroReadingSeriesView, MacroReadingTrendView, MacroReadingView, MacroSeriesView, MacroView } from '../api/types'
import { AppShell } from '../components/AppShell'
import { LoadingIndicator, LoadingPage } from '../components/LoadingIndicator'
import { PageState } from '../components/PageState'
import { StaleBadge } from '../components/StaleBadge'
import { TradingViewButton } from '../components/TradingViewButton'
import { Alert, AlertDescription, AlertTitle } from '../components/ui/alert'
import { Badge } from '../components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/card'
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from '../components/ui/chart'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../components/ui/table'
import { EMPTY, formatJstDateTime, formatNumber, formatPct } from '../lib/format'
import { LABEL } from '../lib/labels'
import { EXTREME_Z_SCORE, readingCategories, readingHealth, readingStatistics, seriesWindowSummary, type ReadingHealth } from '../lib/macro'
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

function TrendCell({ trend }: { trend: MacroReadingTrendView | null }) {
  if (!trend) return <span className="text-muted-foreground">{EMPTY}</span>
  // Neutral direction only: a rising series is not "good" and a falling one is not "bad".
  const Icon = trend.direction === 'up' ? ArrowUp : trend.direction === 'down' ? ArrowDown : Minus
  return (
    <span className="inline-flex items-center gap-0.5 whitespace-nowrap text-xs text-muted-foreground tabular-nums" title={`${trend.anchor_observed_at} の ${fmtValue(trend.anchor_value)} 比`}>
      {trend.months}m<Icon aria-hidden="true" className="size-3" />{fmtValue(Math.abs(trend.change))}
    </span>
  )
}

function ReadingRow({ series }: { series: MacroReadingSeriesView }) {
  const stats = readingStatistics(series)
  return (
    <TableRow>
      <TableCell className="pl-5 sm:pl-6">
        <p className="text-sm font-medium">{series.name}</p>
        <p className="font-mono text-[10px] text-muted-foreground">{series.series_id} · {series.unit} · {series.frequency} · {series.geography}</p>
      </TableCell>
      <TableCell className="text-right font-mono font-semibold tabular-nums">{series.latest_value === null ? EMPTY : fmtValue(series.latest_value)}</TableCell>
      <TableCell className="whitespace-nowrap font-mono text-xs tabular-nums">
        {series.observed_at ?? EMPTY}
        {series.stale && <StaleBadge className="ml-2" detail={series.staleness_days === null ? undefined : `${series.staleness_days} 日`} />}
      </TableCell>
      <TableCell><TrendCell trend={series.short_trend} /></TableCell>
      <TableCell><TrendCell trend={series.long_trend} /></TableCell>
      {/* 1 decimal: rounding to whole percent would flatten 99.7% into "100%" exactly where
          the historical position matters most. */}
      <TableCell className="text-right font-mono tabular-nums">{stats.percentilePct === null ? EMPTY : formatPct(stats.percentilePct)}</TableCell>
      <TableCell className="text-right font-mono tabular-nums">{stats.zScore === null ? EMPTY : fmtValue(stats.zScore)}</TableCell>
      <TableCell className="whitespace-nowrap font-mono text-xs text-muted-foreground tabular-nums">{series.window_years}y / {formatNumber(series.window_observations)} 観測</TableCell>
      <TableCell className="pr-5 sm:pr-6">
        {/* Flags note that a textbook threshold is touched; they are not signals, so they get a
            neutral badge that implies no direction to trade. */}
        <div className="flex flex-wrap gap-1">
          {stats.withheldNote !== null && <Badge variant="outline">{stats.withheldNote}</Badge>}
          {series.flags.map((flag) => <Badge key={flag} variant="outline">{flag}</Badge>)}
        </div>
      </TableCell>
    </TableRow>
  )
}

function HealthList({ title, note, series, detail }: { title: string; note: string; series: readonly MacroReadingSeriesView[]; detail: (series: MacroReadingSeriesView) => string }) {
  return (
    <div className="grid content-start gap-1 rounded-lg border p-4">
      <div className="flex flex-wrap items-center gap-2"><h3 className="text-sm font-semibold">{title}</h3><Badge variant="outline">{series.length} 件</Badge></div>
      <p className="text-xs text-muted-foreground">{note}</p>
      {series.length === 0
        ? <p className="text-sm">該当なし</p>
        : series.map((item) => <p className="font-mono text-xs tabular-nums" key={item.series_id}>{item.series_id} · {detail(item)}</p>)}
    </div>
  )
}

function DataHealthCard({ health }: { health: ReadingHealth }) {
  return (
    <Card className="gap-4 py-5 shadow-sm">
      <CardHeader className="px-5">
        <CardTitle aria-level={3} role="heading">データ健全性 · stale {health.stale.length} 件 / 履歴不足 {health.insufficientHistory.length} 件 / 異常値の疑い {health.extremeZ.length} 件</CardTitle>
        <CardDescription>いずれも値の否定ではなく、読む前に確認する注記である。</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3 px-5 lg:grid-cols-3">
        <HealthList
          detail={(series) => `${series.observed_at ?? EMPTY}・${series.staleness_days ?? EMPTY} 日前（閾値 ${series.staleness_warn_days} 日）`}
          note="観測が閾値より古い。provider の無音の停止を疑う合図。"
          series={health.stale}
          title="stale"
        />
        <HealthList
          detail={(series) => `${series.window_years}y 窓 / ${formatNumber(series.window_observations)} 観測`}
          note="実効窓を履歴が満たさないため percentile / z を出さない。水準比較に使わない。"
          series={health.insufficientHistory}
          title="履歴不足"
        />
        <HealthList
          detail={(series) => `z ${series.z_score === null ? EMPTY : fmtValue(series.z_score)}`}
          note={`|z| ≥ ${EXTREME_Z_SCORE}。異常値の疑い（誤値または真の極値）で、どちらかは一次情報と突き合わせて判断する。`}
          series={health.extremeZ}
          title="異常値の疑い"
        />
      </CardContent>
    </Card>
  )
}

function ReadingPanel({ reading }: { reading: MacroReadingView }) {
  const series = reading.series ?? []
  const health = readingHealth(series)
  return (
    <section className="grid gap-5">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">機械読み値</h1>
        <p className="mt-1 text-sm text-muted-foreground">登録全系列の記述統計と鮮度。regime 分類も売買 signal も含まない。</p>
        <p className="mt-1 font-mono text-xs text-muted-foreground tabular-nums">rules {reading.rules_revision} · {LABEL.asOf} {reading.asof} · {series.length} 系列</p>
      </div>
      <DataHealthCard health={health} />
      {readingCategories(series).map((group) => (
        <Card className="gap-0 overflow-hidden py-0 shadow-sm" key={group.category}>
          <CardHeader className="flex flex-row items-center justify-between gap-4 border-b px-5 py-4">
            <CardTitle aria-level={3} className="font-mono uppercase" role="heading">{group.category}</CardTitle>
            <Badge variant="secondary">{group.series.length} 系列</Badge>
          </CardHeader>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="pl-5 sm:pl-6">系列</TableHead>
                <TableHead className="text-right">最新値</TableHead>
                <TableHead>観測日</TableHead>
                <TableHead>短期</TableHead>
                <TableHead>長期</TableHead>
                <TableHead className="text-right">percentile</TableHead>
                <TableHead className="text-right">z</TableHead>
                <TableHead>実効窓</TableHead>
                <TableHead className="pr-5 sm:pr-6">注記</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {group.series.map((item) => <ReadingRow key={item.series_id} series={item} />)}
            </TableBody>
          </Table>
        </Card>
      ))}
    </section>
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
  const [reading, setReading] = useState<MacroReadingView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [period, setPeriod] = useState<MacroPeriod>('max')
  const [granularity, setGranularity] = useState<MacroGranularity>('monthly')
  const [loading, setLoading] = useState(true)
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set())

  // The reading is recomputed from the indicator store for one as-of date, so it does not
  // depend on the panel's period / granularity and is fetched once. An unavailable reading
  // (404 with no indicator store) hides its own panel and leaves the rest of the page
  // readable rather than failing the whole tab.
  useEffect(() => {
    const controller = new AbortController()
    fetchJson<MacroReadingView>('/api/macro/reading', { signal: controller.signal })
      .then(setReading)
      .catch(() => {
        setReading(null)
      })
    return () => controller.abort()
  }, [])

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
      {reading !== null && <ReadingPanel reading={reading} />}

      <section className="grid gap-3">
        <h2 className="text-2xl font-semibold tracking-tight">経済分析レポート</h2>
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
