import { useEffect, useId, useState, type ReactNode } from 'react'
import { Link } from 'react-router'
import { ArrowDown, ArrowRight, ArrowUp, CircleAlert, Minus, Search } from 'lucide-react'
import { Area, AreaChart, CartesianGrid, Line, LineChart, XAxis, YAxis } from 'recharts'

import { fetchJson } from '../api/client'
import type { MacroContextExcerptView, MacroPointView, MacroReadingSeriesView, MacroReadingTrendView, MacroSeriesView, MacroView } from '../api/types'
import { InfoHint } from '../components/InfoHint'
import { LoadingIndicator, LoadingPage } from '../components/LoadingIndicator'
import { PageShell } from '../components/PageShell'
import { PageState } from '../components/PageState'
import { SectionCard } from '../components/SectionCard'
import { StaleBadge } from '../components/StaleBadge'
import { TradingViewButton } from '../components/TradingViewButton'
import { UpdatedAtBadge } from '../components/UpdatedAtBadge'
import { Alert, AlertDescription, AlertTitle } from '../components/ui/alert'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Card } from '../components/ui/card'
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from '../components/ui/chart'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '../components/ui/dialog'
import { Input } from '../components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../components/ui/table'
import { EMPTY, formatJstDateTime, formatNumber, formatPct } from '../lib/format'
import { LABEL } from '../lib/labels'
import { EXTREME_Z_SCORE, INDICATOR_STATUSES, INDICATOR_STATUS_LABEL, buildIndicatorGroups, filterIndicatorGroups, macroSeriesHistoryUrl, readingStatistics, seriesWindowSummary, statisticName, summarizeIndicators, transformSeriesHistory, type MacroHistoryGranularity, type MacroHistoryPeriod, type MacroIndicatorRow, type MacroIndicatorStatus } from '../lib/macro'
import { cn } from '../lib/utils'

type MacroPeriod = MacroHistoryPeriod
type MacroGranularity = MacroHistoryGranularity

const PERIOD_LABEL: Readonly<Record<MacroPeriod, string>> = { '1y': '1年', '5y': '5年', '10y': '10年', max: '全期間' }
const GRANULARITY_LABEL: Readonly<Record<MacroGranularity, string>> = { daily: '日次', weekly: '週次', monthly: '月次', yearly: '年次' }

// Every term the table uses that needs a sentence to be read correctly. They live next to
// their own label behind an ⓘ, so the numbers are never fenced off by a paragraph.
const HINT = {
  panel: '登録全系列の記述統計と鮮度。regime 分類も売買 signal も含まない。行をクリックすると拡大チャートと全項目が開く。',
  chart: '一覧は約1年・月次・最大13点の軽量推移。画面右上の期間・粒度は、行を開いた詳細チャートだけに適用する。percentile と z の実効窓は系列ごとに決まり、どちらの表示期間とも一致しない。',
  statistic: 'percentile と z が順位づける値。水準はその系列の値そのもの、前年比は 12 か月前比の変化率で、水準に位置の意味がない系列に使う。',
  percentile: '実効窓の分布のうち、統計の値以下だった観測の割合。窓は系列ごと（3y / 10y など）で、左のチャートの期間とは一致しない。',
  zScore: `実効窓の平均から標準偏差いくつ離れているか。|z| ≥ ${EXTREME_Z_SCORE} は分布の端という読み値で、値の否定ではない。`,
  window: 'percentile と z を取る窓の年数と、その中の観測数（分母は頻度が示唆する件数）。窓を履歴が満たさない系列は percentile と z を出さない。',
  notes: '取得失敗・stale・履歴不足・分布の端と、教科書的な閾値に触れたことを示す flag。いずれも signal ではない。',
  fetchFailed: '直近の取得試行が失敗した系列。store は前回値を返し続けるので、stale になる前に provider の停止を捉える唯一の合図。',
  stale: '観測が系列ごとの閾値より古い。provider の無音の停止を疑う合図で、percentile の解釈可能性を壊す。',
  insufficientHistory: '実効窓を履歴が満たさない（開始が遅い・件数不足・欠落が多い）ため percentile と z を出さない。水準比較に使わない。',
  extreme: `|z| ≥ ${EXTREME_Z_SCORE}。実効窓の分布の端にいるという読み値そのもので、健全性の問題ではない。誤値でないことは経済分析レポートが一次情報と突き合わせて確認する。`,
} as const

const STATUS_HINT: Readonly<Record<MacroIndicatorStatus, string>> = {
  'fetch-failed': HINT.fetchFailed,
  stale: HINT.stale,
  'insufficient-history': HINT.insufficientHistory,
  extreme: HINT.extreme,
}

function fmtValue(value: number): string {
  const magnitude = Math.abs(value)
  const digits = magnitude >= 100 ? 0 : magnitude >= 1 ? 2 : 3
  return formatNumber(value, digits)
}

function statisticText(series: MacroReadingSeriesView): string {
  const name = statisticName(series.statistic)
  // The level itself is already in 最新値, so only a transformed statistic repeats a value.
  if (series.statistic === 'level') return name
  const value = series.statistic_value === null ? EMPTY : fmtValue(series.statistic_value)
  return `${name} ${value}${series.statistic_unit === 'percent' ? '%' : ''}`
}

// The estimate is mechanical, from the series cadence and its publication lag, so a due
// date already behind us is a normal state and reads as overdue rather than as a negative
// number of days ahead.
function printDueText(days: number): string {
  if (days > 0) return `${days} 日後`
  if (days === 0) return '本日'
  return `${-days} 日超過`
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

// The four states plus any threshold flags, as one set of badges. They are the row's only
// marker of a problem, so they sit in the notes column on wide screens and directly under
// the series name where that column is dropped.
function StatusBadges({ row, className }: { row: MacroIndicatorRow; className?: string }) {
  const reading = row.reading
  const stats = reading === null ? null : readingStatistics(reading)
  // Insufficient history is already a state, so only the other withheld reason adds a badge.
  const withheld = reading?.insufficient_history === false ? stats?.withheldNote ?? null : null
  // Nothing to say leaves no element behind, so a row without notes has no stray gap.
  if (row.statuses.length === 0 && withheld === null && (reading?.flags.length ?? 0) === 0) return null
  return (
    <div className={cn('flex flex-wrap gap-1', className)}>
      {row.statuses.map((status) => (
        status === 'stale'
          ? <StaleBadge detail={reading?.staleness_days === null || reading === null ? undefined : `${reading.staleness_days} 日`} key={status} />
          // Flags and edges note that something is worth looking at; they imply no
          // direction to trade, so only a failed fetch gets a red badge.
          : <Badge key={status} variant={status === 'fetch-failed' ? 'destructive' : 'outline'}>{INDICATOR_STATUS_LABEL[status]}</Badge>
      ))}
      {/* The remaining reason a statistic is withheld: a year-on-year change with no
          comparable observation a year back. */}
      {withheld !== null && <Badge variant="outline">{withheld}</Badge>}
      {reading?.flags.map((flag) => <Badge key={flag} variant="outline">{flag}</Badge>)}
    </div>
  )
}

function Sparkline({ points }: { points: readonly MacroPointView[] }) {
  // The gradient belongs to this chart's own SVG under an id React guarantees is unique,
  // so a sparkline placed anywhere carries its fill with it.
  const fillId = useId()
  // A fixed size instead of a responsive container: the column is a fixed width and the
  // table draws a hundred of these, so measuring each one buys nothing.
  if (points.length === 0) return <div aria-label="観測なし" className="grid h-8 w-24 place-items-center text-xs text-muted-foreground">{EMPTY}</div>
  return (
    <AreaChart data={points as MacroPointView[]} height={32} margin={{ top: 2, right: 2, bottom: 2, left: 2 }} width={96}>
      <defs>
        <linearGradient id={fillId} x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stopColor="var(--brand-lime)" stopOpacity={0.45} />
          <stop offset="100%" stopColor="var(--brand-lime)" stopOpacity={0.04} />
        </linearGradient>
      </defs>
      <YAxis domain={['auto', 'auto']} hide />
      <Area
        // Without a base, recharts anchors the fill at zero — or at the top of the plot for
        // an all-negative series, which paints the band above the line. Macro carries
        // spreads and z-scores that sit below zero, so the base is pinned to the plot floor
        // and every series reads the same way.
        baseValue="dataMin"
        dataKey="value"
        dot={false}
        fill={`url(#${fillId})`}
        // recharts fills an area at 0.6 by default, which would multiply the gradient's
        // own stops; the gradient alone decides how the band fades.
        fillOpacity={1}
        isAnimationActive={false}
        stroke="var(--chart-1)"
        strokeWidth={1.5}
        type="monotone"
      />
    </AreaChart>
  )
}

function FullChart({ series }: { series: MacroSeriesView }) {
  const config = { value: { label: series.label, color: 'var(--chart-1)' } } satisfies ChartConfig
  if (series.points.length === 0) return <p className="py-12 text-center text-sm text-muted-foreground">観測値なし</p>
  // The right margin holds the last date label inside the plot, and the clip keeps any
  // label recharts still places past the edge out of the dialog's padding.
  return (
    <ChartContainer className="h-56 w-full overflow-hidden" config={config}>
      <LineChart data={series.points} margin={{ left: 4, right: 34 }}>
        <CartesianGrid vertical={false} />
        <XAxis axisLine={false} dataKey="observed_at" minTickGap={28} tickLine={false} />
        <YAxis axisLine={false} domain={['auto', 'auto']} tickLine={false} width={52} />
        <ChartTooltip content={<ChartTooltipContent />} />
        <Line dataKey="value" dot={false} stroke="var(--color-value)" strokeWidth={2} type="monotone" />
      </LineChart>
    </ChartContainer>
  )
}

function IndicatorRow({ row, onOpen }: { row: MacroIndicatorRow; onOpen: () => void }) {
  const { series, reading } = row
  const stats = reading === null ? null : readingStatistics(reading)
  const summary = seriesWindowSummary(series.points)
  return (
    <TableRow className="cursor-pointer" onClick={onOpen}>
      <TableCell className="w-[116px] pl-3 sm:w-[124px] sm:pl-6"><Sparkline points={series.points} /></TableCell>
      {/* Table cells do not wrap by default; the name and its id are the only text here
          long enough to need it, and holding them on one line would push the value off a
          narrow screen. */}
      <TableCell className="whitespace-normal">
        {/* The row is clickable for the mouse; this button is what a keyboard reaches. */}
        <button className="text-left text-sm font-medium underline-offset-2 hover:underline focus-visible:underline focus-visible:outline-none" onClick={onOpen} type="button">
          {series.name}
        </button>
        <p className="font-mono text-[10px] text-muted-foreground">{series.series_id} · {series.unit}</p>
        <StatusBadges className="mt-1 sm:hidden" row={row} />
      </TableCell>
      <TableCell className="text-right font-mono font-semibold tabular-nums">
        {reading?.latest_value == null ? (summary.latest === null ? EMPTY : fmtValue(summary.latest)) : fmtValue(reading.latest_value)}
        {/* Narrow screens have no room for two trend columns beside the value, so the two
            move under it rather than off the edge of a table nobody scrolls sideways. */}
        <span className="mt-0.5 grid justify-items-end font-normal sm:hidden">
          <TrendCell trend={reading?.short_trend ?? null} />
          <TrendCell trend={reading?.long_trend ?? null} />
        </span>
      </TableCell>
      <TableCell className="hidden whitespace-nowrap font-mono text-xs tabular-nums sm:table-cell">{reading?.observed_at ?? EMPTY}</TableCell>
      <TableCell className="hidden sm:table-cell"><TrendCell trend={reading?.short_trend ?? null} /></TableCell>
      <TableCell className="hidden sm:table-cell"><TrendCell trend={reading?.long_trend ?? null} /></TableCell>
      <TableCell className="hidden whitespace-nowrap font-mono text-xs tabular-nums lg:table-cell">{reading === null ? EMPTY : statisticText(reading)}</TableCell>
      {/* 1 decimal: rounding to whole percent would flatten 99.7% into "100%" exactly where
          the historical position matters most. */}
      <TableCell className="hidden text-right font-mono tabular-nums sm:table-cell">{stats?.percentilePct == null ? EMPTY : formatPct(stats.percentilePct)}</TableCell>
      <TableCell className="hidden text-right font-mono tabular-nums lg:table-cell">{stats?.zScore == null ? EMPTY : fmtValue(stats.zScore)}</TableCell>
      {/* The implied count (monthly / weekly / quarterly) shows how full the window is:
          a window with holes describes the periods it happens to hold, not the decade. */}
      <TableCell className="hidden whitespace-nowrap font-mono text-xs text-muted-foreground tabular-nums xl:table-cell">{reading === null ? EMPTY : `${reading.window_years}y / ${formatNumber(reading.window_observations)}${reading.expected_observations === null ? '' : `/${formatNumber(reading.expected_observations)}`} 観測`}</TableCell>
      <TableCell className="hidden pr-5 sm:table-cell sm:pr-6"><StatusBadges row={row} /></TableCell>
    </TableRow>
  )
}

function DetailField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-[10px] font-semibold tracking-wide text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 font-mono text-sm tabular-nums">{children}</dd>
    </div>
  )
}

function IndicatorDialog({ row, history, historyLoading, historyError, period, granularity, onClose }: { row: MacroIndicatorRow | null; history: MacroSeriesView | null; historyLoading: boolean; historyError: boolean; period: MacroPeriod; granularity: MacroGranularity; onClose: () => void }) {
  if (row === null) return null
  const { reading, failedFetch } = row
  const series = history === null ? row.series : transformSeriesHistory(history, period, granularity)
  const stats = reading === null ? null : readingStatistics(reading)
  return (
    <Dialog onOpenChange={(open) => { if (!open) onClose() }} open>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>{series.name}</DialogTitle>
          <DialogDescription className="font-mono text-xs">
            {series.series_id} · {series.unit}
            {reading !== null && ` · ${reading.frequency} · ${reading.geography} · ${reading.category}`}
          </DialogDescription>
        </DialogHeader>
        <StatusBadges row={row} />
        {failedFetch !== null && (
          <Alert variant="destructive">
            <CircleAlert />
            <AlertTitle>取得失敗 · {formatJstDateTime(failedFetch.finished_at)}</AlertTitle>
            <AlertDescription className="break-all">{failedFetch.error_message ?? '理由の記録なし'}</AlertDescription>
          </Alert>
        )}
        <div className="grid gap-1">
          {historyError ? <Alert variant="destructive"><CircleAlert /><AlertTitle>系列履歴を取得できませんでした</AlertTitle><AlertDescription>系列を開き直して再度お試しください。</AlertDescription></Alert> : historyLoading || history === null ? <div className="grid h-56 place-items-center"><LoadingIndicator label="系列履歴を読み込んでいます" /></div> : <FullChart series={series} />}
          {history !== null && !historyLoading && !historyError && <p className="text-xs text-muted-foreground">
            チャートは {PERIOD_LABEL[period]} / {GRANULARITY_LABEL[granularity]}
            {reading !== null && `。percentile と z の実効窓は ${reading.window_years}y で、この期間とは別`}。
          </p>}
        </div>
        {series.tradingview_symbol !== null && <div><TradingViewButton labeled name={series.label} symbol={series.tradingview_symbol} /></div>}
        <dl className="grid grid-cols-2 gap-x-5 gap-y-3 sm:grid-cols-3">
          <DetailField label="最新値">{reading?.latest_value == null ? EMPTY : fmtValue(reading.latest_value)}</DetailField>
          <DetailField label="観測日">{reading?.observed_at ?? EMPTY}{reading?.staleness_days != null && <span className="ml-1 text-xs text-muted-foreground">（{reading.staleness_days} 日前 / 閾値 {reading.staleness_warn_days} 日）</span>}</DetailField>
          <DetailField label="次回公表（推定）">{reading?.next_print_estimate ?? EMPTY}{reading?.print_due_in_days != null && <span className="ml-1 text-xs text-muted-foreground">（{printDueText(reading.print_due_in_days)}）</span>}</DetailField>
          <DetailField label="短期トレンド"><TrendCell trend={reading?.short_trend ?? null} /></DetailField>
          <DetailField label="長期トレンド"><TrendCell trend={reading?.long_trend ?? null} /></DetailField>
          <DetailField label="統計">{reading === null ? EMPTY : statisticText(reading)}</DetailField>
          <DetailField label="percentile">{stats?.percentilePct == null ? EMPTY : formatPct(stats.percentilePct)}</DetailField>
          <DetailField label="z">{stats?.zScore == null ? EMPTY : fmtValue(stats.zScore)}</DetailField>
          <DetailField label="実効窓">{reading === null ? EMPTY : `${reading.window_years}y / ${formatNumber(reading.window_observations)}${reading.expected_observations === null ? '' : `/${formatNumber(reading.expected_observations)}`} 観測`}</DetailField>
        </dl>
      </DialogContent>
    </Dialog>
  )
}

function LatestContext({ context }: { context: MacroContextExcerptView | null }) {
  if (context === null) return <Alert><CircleAlert /><AlertTitle>Macro Context なし</AlertTitle><AlertDescription>機械読み値は確認できますが、現在の環境判断はまだ発行されていません。</AlertDescription></Alert>
  return (
    <SectionCard
      description={`${LABEL.asOf} ${context.as_of} / ${context.age_days} 日前`}
      meta={<div className="flex gap-2">{context.stale && <StaleBadge />}<Link className="text-sm underline underline-offset-4" to={`/macro/reports/${context.context_id}`}>詳細レポート</Link></div>}
      title="現在のマクロ局面"
    >
      <div className="grid gap-5 px-5 py-4 sm:px-6">
        {context.warnings.length > 0 && <div className="flex flex-wrap gap-1.5">{context.warnings.map((warning) => <Badge key={warning} variant="outline">{warning}</Badge>)}</div>}
        <p className="whitespace-pre-wrap text-sm leading-7">{context.summary}</p>
        {context.synthesis !== null && (
          <div className="grid gap-3 md:grid-cols-2">
            <div><h4 className="text-sm font-semibold">支配的な力</h4><ul className="mt-1 grid gap-2">{context.synthesis.dominant_forces.map((force) => <li key={force.force_id}><p className="text-sm font-medium">{force.title}</p><p className="text-sm text-muted-foreground">{force.summary}</p><p className="text-xs text-muted-foreground">伝播: {force.transmission}</p></li>)}</ul></div>
            <div><h4 className="text-sm font-semibold">相互作用</h4><ul className="mt-1 grid gap-2">{context.synthesis.interactions.map((item, index) => <li className="text-sm text-muted-foreground" key={`${item.force_ids.join('-')}-${index}`}>{item.summary}</li>)}</ul></div>
          </div>
        )}
        {context.risk_environment !== null && <div><h4 className="text-sm font-semibold">リスク環境 · {context.risk_environment.stance} / {context.risk_environment.confidence}</h4><p className="mt-1 text-sm text-muted-foreground">{context.risk_environment.summary}</p><ul className="mt-1 list-disc pl-5 text-sm text-muted-foreground">{context.risk_environment.falsifiers.map((item) => <li key={item}>{item}</li>)}</ul></div>}
        {context.scenarios.length > 0 && <div><h4 className="text-sm font-semibold">シナリオ</h4><div className="mt-1 grid gap-2 sm:grid-cols-3">{context.scenarios.map((scenario) => <Card className="p-3" key={scenario.case}><p className="text-sm font-medium">{scenario.case}{scenario.probability === null ? '' : ` · ${formatPct(scenario.probability * 100)}`}</p><p className="mt-1 text-xs text-muted-foreground">{scenario.summary}</p></Card>)}</div></div>}
        <details className="rounded-md border p-3" open={false}>
          <summary className="cursor-pointer text-sm font-semibold">Research への接続</summary>
          <div className="mt-3 grid gap-4">
            <div><h5 className="text-xs font-semibold">優先確認</h5><ul className="mt-1 list-disc pl-5 text-sm text-muted-foreground">{context.research_priority_hints.map((item) => <li key={`${item.applies_to}-${item.summary}`}>{item.summary}（{item.applies_to}）</li>)}</ul></div>
            {context.bargain_topography !== null && <div><h5 className="text-xs font-semibold">割安地形</h5><p className="text-sm text-muted-foreground">{context.bargain_topography.summary}</p></div>}
            <div><h5 className="text-xs font-semibold">見積り上の注意</h5><ul className="mt-1 list-disc pl-5 text-sm text-muted-foreground">{context.estimate_caveats.map((item) => <li key={`${item.applies_to}-${item.affected_component}`}>{item.summary}（{item.affected_component} / {item.applies_to} / {item.materiality}）</li>)}</ul></div>
            {context.sizing_cautions.length > 0 && <div><h5 className="text-xs font-semibold">Sizing caution</h5><ul className="mt-1 list-disc pl-5 text-sm text-muted-foreground">{context.sizing_cautions.map((item) => <li key={item.summary}>{item.summary}</li>)}</ul></div>}
          </div>
        </details>
      </div>
    </SectionCard>
  )
}

export function MacroPage() {
  const [data, setData] = useState<MacroView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [period, setPeriod] = useState<MacroPeriod>('max')
  const [granularity, setGranularity] = useState<MacroGranularity>('monthly')
  const [loading, setLoading] = useState(true)
  const [query, setQuery] = useState('')
  const [statuses, setStatuses] = useState<ReadonlySet<MacroIndicatorStatus>>(new Set())
  const [openSeriesId, setOpenSeriesId] = useState<string | null>(null)
  const [history, setHistory] = useState<MacroSeriesView | null>(null)
  const [historyLoading, setHistoryLoading] = useState(false)
  const [historyError, setHistoryError] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    setError(null)
    setLoading(true)
    fetchJson<MacroView>('/api/macro', { signal: controller.signal })
      .then(setData)
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === 'AbortError') return
        setError(reason instanceof Error ? reason.message : 'Macro を読み込めませんでした')
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [])

  useEffect(() => {
    setHistoryError(false)
    const historyUrl = macroSeriesHistoryUrl(openSeriesId)
    if (historyUrl === null) {
      setHistory(null)
      setHistoryLoading(false)
      return
    }
    const controller = new AbortController()
    setHistory(null)
    setHistoryLoading(true)
    fetchJson<MacroSeriesView>(historyUrl, { signal: controller.signal })
      .then((value) => { if (!controller.signal.aborted) setHistory(value) })
      .catch((reason: unknown) => {
        if (controller.signal.aborted || (reason instanceof DOMException && reason.name === 'AbortError')) return
        setHistoryError(true)
      })
      .finally(() => { if (!controller.signal.aborted) setHistoryLoading(false) })
    return () => controller.abort()
  }, [openSeriesId])

  if (error) return <PageState message={error} />
  if (!data) return <LoadingPage label="Macro を読み込んでいます" />

  // Degrade gracefully rather than white-screen if a served view is ever missing a
  // field (e.g. a stale view during a deploy that precedes its re-materialization).
  const reports = data.reports ?? []
  const groups = buildIndicatorGroups(data.groups ?? [], data.reading)
  const summary = summarizeIndicators(groups)
  const visibleGroups = filterIndicatorGroups(groups, { query, statuses })
  const openRow = groups.flatMap((group) => group.rows).find((row) => row.series.series_id === openSeriesId) ?? null

  const toggleStatus = (status: MacroIndicatorStatus) => {
    setStatuses((current) => {
      const next = new Set(current)
      if (next.has(status)) next.delete(status)
      else next.add(status)
      return next
    })
  }

  return (
    <PageShell meta={<UpdatedAtBadge value={data.latest_context?.published_at ?? null} />} title="マクロ経済分析">
      <LatestContext context={data.latest_context} />

      <section className="grid gap-4">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div className="flex items-center gap-2">
            <h2 className="text-xl font-semibold tracking-tight">マクロ経済指標</h2>
            <InfoHint label="マクロ経済指標">{HINT.panel}</InfoHint>
            <Badge variant="secondary">{formatNumber(summary.seriesCount)} 系列</Badge>
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <label className="grid gap-1"><span className="text-xs font-medium text-muted-foreground">詳細期間</span><Select onValueChange={(value) => setPeriod(value as MacroPeriod)} value={period}><SelectTrigger aria-label="詳細チャートの表示期間" className="w-24"><SelectValue /></SelectTrigger><SelectContent>{Object.entries(PERIOD_LABEL).map(([value, text]) => <SelectItem key={value} value={value}>{text}</SelectItem>)}</SelectContent></Select></label>
            <label className="grid gap-1"><span className="text-xs font-medium text-muted-foreground">詳細粒度</span><Select onValueChange={(value) => setGranularity(value as MacroGranularity)} value={granularity}><SelectTrigger aria-label="詳細チャートの表示粒度" className="w-28"><SelectValue /></SelectTrigger><SelectContent>{Object.entries(GRANULARITY_LABEL).map(([value, text]) => <SelectItem key={value} value={value}>{text}</SelectItem>)}</SelectContent></Select></label>
            {loading && <LoadingIndicator className="self-end pb-2" label="マクロ経済指標を更新しています" size={24} />}
          </div>
        </div>

        {/* Wider between the filters than inside one, so each ⓘ reads as belonging to the
            button on its left. */}
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <div className="relative w-full sm:w-72">
            <Search aria-hidden="true" className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input aria-label="系列を検索" className="pl-8" onChange={(event) => setQuery(event.target.value)} placeholder="系列名 / series_id で検索" value={query} />
          </div>
          {/* Count and filter in one control: the number says how many rows carry the
              classification, and pressing it shows them. The hint sits beside the button
              rather than inside it — an interactive element nested in another is invalid
              and unreachable by keyboard. */}
          {INDICATOR_STATUSES.map((status) => (
            <div className="flex items-center gap-1" key={status}>
              <Button
                aria-pressed={statuses.has(status)}
                // A classification with no rows would filter the table down to nothing;
                // a selected one stays clickable so it can always be turned off.
                disabled={summary.counts[status] === 0 && !statuses.has(status)}
                onClick={() => toggleStatus(status)}
                size="sm"
                variant={statuses.has(status) ? 'default' : 'outline'}
              >
                {INDICATOR_STATUS_LABEL[status]} {summary.counts[status]}
              </Button>
              <InfoHint label={INDICATOR_STATUS_LABEL[status]}>{STATUS_HINT[status]}</InfoHint>
            </div>
          ))}
        </div>

        {visibleGroups.length === 0
          ? <p className="text-sm text-muted-foreground">条件に合う系列はありません。</p>
          : visibleGroups.map((group) => (
            <SectionCard headingLevel={3} key={group.title} meta={<Badge variant="secondary">{group.rows.length} 系列</Badge>} title={group.title}>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="pl-5 sm:pl-6"><span className="inline-flex items-center gap-1">推移（約1年）<InfoHint label="推移">{HINT.chart}</InfoHint></span></TableHead>
                    <TableHead>系列</TableHead>
                    <TableHead className="text-right">最新値</TableHead>
                    <TableHead className="hidden sm:table-cell">観測日</TableHead>
                    <TableHead className="hidden sm:table-cell">短期</TableHead>
                    <TableHead className="hidden sm:table-cell">長期</TableHead>
                    <TableHead className="hidden lg:table-cell"><span className="inline-flex items-center gap-1">統計<InfoHint label="統計">{HINT.statistic}</InfoHint></span></TableHead>
                    <TableHead className="hidden text-right sm:table-cell"><span className="inline-flex items-center gap-1">percentile<InfoHint label="percentile">{HINT.percentile}</InfoHint></span></TableHead>
                    <TableHead className="hidden text-right lg:table-cell"><span className="inline-flex items-center gap-1">z<InfoHint label="z">{HINT.zScore}</InfoHint></span></TableHead>
                    <TableHead className="hidden xl:table-cell"><span className="inline-flex items-center gap-1">実効窓<InfoHint label="実効窓">{HINT.window}</InfoHint></span></TableHead>
                    <TableHead className="hidden pr-5 sm:table-cell sm:pr-6"><span className="inline-flex items-center gap-1">注記<InfoHint label="注記">{HINT.notes}</InfoHint></span></TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {group.rows.map((row) => <IndicatorRow key={row.series.series_id} onOpen={() => setOpenSeriesId(row.series.series_id)} row={row} />)}
                </TableBody>
              </Table>
            </SectionCard>
          ))}
      </section>

      <details className="rounded-lg border bg-card shadow-sm">
        <summary className="cursor-pointer px-5 py-4 text-lg font-semibold sm:px-6">過去の経済分析レポート（{reports.length}）</summary>
        {reports.length === 0
          ? <p className="border-t px-5 py-4 text-sm text-muted-foreground sm:px-6">発行済みレポートはありません。</p>
          : <div className="divide-y border-t">{reports.map((report) => (
            <Link className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 px-5 py-3 transition-colors hover:bg-muted/40 sm:px-6" key={report.context_id} to={`/macro/reports/${report.context_id}`}>
              <div className="flex min-w-0 items-center gap-2"><span className="truncate text-sm font-medium">{report.summary}</span>{report.stale && <StaleBadge />}</div>
              <div className="flex shrink-0 items-center gap-3 font-mono text-xs text-muted-foreground tabular-nums"><span>{LABEL.asOf} {report.as_of}</span><UpdatedAtBadge className="hidden sm:inline-flex" compact value={report.published_at} /><ArrowRight aria-hidden="true" className="size-4" /></div>
            </Link>
          ))}</div>}
      </details>

      <IndicatorDialog granularity={granularity} history={history} historyError={historyError} historyLoading={historyLoading} onClose={() => setOpenSeriesId(null)} period={period} row={openRow} />
    </PageShell>
  )
}
