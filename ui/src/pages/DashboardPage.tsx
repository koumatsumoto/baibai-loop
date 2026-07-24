import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { CalendarCheck2, CalendarClock, CalendarDays, CircleAlert } from 'lucide-react'
import { Pie, PieChart } from 'recharts'

import { fetchJson } from '../api/client'
import type {
  DashboardView,
  HoldingView,
  OperationSessionView,
  PortfolioOutcomeView,
  OperationsView,
  ProposalView,
  TaskView,
  UpcomingEventView,
  WarningView,
} from '../api/types'
import { AppShell } from '../components/AppShell'
import { AsOfBadge } from '../components/AsOfBadge'
import { PageState } from '../components/PageState'
import { PctBadge } from '../components/PctBadge'
import { StaleBadge } from '../components/StaleBadge'
import { TradingViewButton } from '../components/TradingViewButton'
import { YenAmount } from '../components/YenAmount'
import { Alert, AlertDescription, AlertTitle } from '../components/ui/alert'
import { Badge } from '../components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/card'
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from '../components/ui/chart'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../components/ui/table'
import { EMPTY, formatJstDate, formatPct, formatYen } from '../lib/format'
import { cn } from '../lib/utils'

const allocationConfig = {
  holdings: { label: '保有株式', color: 'var(--chart-1)' },
  available: { label: '購入余力', color: 'var(--chart-2)' },
  reserved: { label: '予約', color: 'var(--chart-3)' },
} satisfies ChartConfig

function NextCard({ label, task, event = false }: { label: string; task: TaskView | null; event?: boolean }) {
  const date = event ? task?.event_date ?? task?.due_date ?? null : task?.due_date ?? null
  const Icon = event ? CalendarDays : CalendarCheck2

  return (
    <Card className="gap-4 py-5 shadow-sm">
      <CardHeader className="flex flex-row items-center justify-between px-5">
        <div className="flex items-center gap-2 text-xs font-semibold tracking-wide text-muted-foreground">
          <Icon className="size-4" aria-hidden="true" />
          <span>{label}</span>
        </div>
        {task?.overdue && <Badge variant="destructive">期限超過</Badge>}
      </CardHeader>
      <CardContent className="px-5">
        {task ? (
          <div className="grid gap-3">
            <time className="w-fit rounded-md bg-muted px-2.5 py-1.5 font-mono text-sm font-semibold tabular-nums text-foreground ring-1 ring-foreground/10" dateTime={date ?? undefined}>{formatJstDate(date)}</time>
            <p className="font-medium leading-snug">{event && task.event_label ? task.event_label : task.title}</p>
            {event && task.event_label && <p className="truncate text-xs text-muted-foreground">{task.title}</p>}
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">なし</p>
        )}
      </CardContent>
    </Card>
  )
}

function PortfolioAllocationCard({ data }: { data: DashboardView }) {
  const allocation = [
    { key: 'holdings', name: '保有株式', value: Math.max(data.holdings_market_value_yen ?? 0, 0), fill: 'var(--color-holdings)' },
    { key: 'available', name: '購入余力', value: Math.max(data.available_cash_yen ?? 0, 0), fill: 'var(--color-available)' },
    { key: 'reserved', name: '予約', value: Math.max(data.reserved_cash_yen ?? 0, 0), fill: 'var(--color-reserved)' },
  ]
  const hasAllocation = allocation.some((item) => item.value > 0)

  return (
    <Card className="overflow-hidden py-0 shadow-sm">
      <div className="grid lg:grid-cols-[minmax(320px,0.8fr)_1.2fr]">
        <div className="border-b p-5 lg:border-r lg:border-b-0 sm:p-6">
          <CardHeader className="px-0 pb-2">
            <CardTitle className="text-base">資産配分</CardTitle>
            <CardDescription>現在の保有株式・購入余力・予約</CardDescription>
          </CardHeader>
          <div className="relative mx-auto h-[230px] max-w-[360px]">
            {hasAllocation ? (
              <ChartContainer className="h-full w-full" config={allocationConfig}>
                <PieChart accessibilityLayer>
                  <ChartTooltip
                    content={<ChartTooltipContent formatter={(value, name) => (
                      <div className="flex w-full min-w-40 items-center justify-between gap-4">
                        <span className="text-muted-foreground">{allocationConfig[String(name) as keyof typeof allocationConfig]?.label ?? String(name)}</span>
                        <span className="font-mono font-medium tabular-nums">{formatYen(Number(value))}</span>
                      </div>
                    )} hideLabel />}
                  />
                  <Pie data={allocation} dataKey="value" innerRadius={72} isAnimationActive={false} nameKey="key" outerRadius={100} paddingAngle={2} strokeWidth={0} />
                </PieChart>
              </ChartContainer>
            ) : (
              <div className="grid h-full place-items-center text-sm text-muted-foreground">配分データなし</div>
            )}
            {hasAllocation && (
              <div className="pointer-events-none absolute inset-0 grid place-content-center text-center">
                <span className="text-xs text-muted-foreground">総資産</span>
                <YenAmount className="mt-1 text-lg font-semibold tracking-tight" value={data.total_capital_yen} />
              </div>
            )}
          </div>
        </div>

        <div className="grid content-center divide-y p-5 sm:p-6">
          {[
            { label: '総資産', value: data.total_capital_yen, detail: `${data.holdings.length} 銘柄を保有`, color: 'bg-foreground' },
            { label: '保有株式', value: data.holdings_market_value_yen, detail: data.deployed_pct === null ? '評価額' : `総資産の ${formatPct(data.deployed_pct)}`, color: 'bg-chart-1' },
            { label: '購入余力', value: data.available_cash_yen, detail: data.cash_pct === null ? '利用可能な現金' : `総資産の ${formatPct(data.cash_pct)}`, color: 'bg-chart-2' },
            { label: '予約', value: data.reserved_cash_yen, detail: data.reserved_pct === null ? '確保済みの現金' : `総資産の ${formatPct(data.reserved_pct)}`, color: 'bg-chart-3' },
          ].map((metric) => (
            <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3 py-5 first:pt-0 last:pb-0" key={metric.label}>
              <div className="flex min-w-0 items-center gap-3">
                <span className={cn('size-2.5 rounded-sm', metric.color)} aria-hidden="true" />
                <div>
                  <p className="text-sm font-medium">{metric.label}</p>
                  <p className="mt-0.5 text-xs text-muted-foreground">{metric.detail}</p>
                </div>
              </div>
              <YenAmount className="shrink-0 text-sm font-semibold tracking-tight sm:text-base" value={metric.value} />
            </div>
          ))}
        </div>
      </div>
    </Card>
  )
}

function warningCopy(warning: WarningView) {
  const actual = formatPct(warning.actual_pct, { digits: 2 })
  const threshold = formatPct(warning.warning_pct, { digits: 2 })
  if (warning.code === 'portfolio.ticker-concentration') {
    return {
      title: `銘柄集中度 · ${warning.key}`,
      description: `構成比 ${actual} が目安 ${threshold} を上回っています。追加購入時に集中度を確認してください。`,
    }
  }
  if (warning.code === 'portfolio.sector-concentration') {
    return {
      title: `業種集中度 · ${warning.key}`,
      description: `構成比 ${actual} が目安 ${threshold} を上回っています。同業種への追加購入時に確認してください。`,
    }
  }
  if (warning.code === 'portfolio.common_factor-concentration') {
    return {
      title: `共通要因の集中度 · ${warning.key}`,
      description: `構成比 ${actual} が目安 ${threshold} を上回っています。同じリスク要因への追加投資時に確認してください。`,
    }
  }
  if (warning.code === 'portfolio.dry-powder') {
    return {
      title: '購入余力',
      description: `購入余力が ${actual} で、目安 ${threshold} を下回っています。新規購入前に確認してください。`,
    }
  }
  return {
    title: 'ポートフォリオ確認事項',
    description: `${warning.key}: ${actual}（目安 ${threshold}）`,
  }
}

function PortfolioWarnings({ warnings }: { warnings: WarningView[] }) {
  if (warnings.length === 0) return null
  return (
    <div className="border-t bg-warning-surface px-5 py-4 sm:px-6" aria-label="ポートフォリオ確認事項">
      <div className="grid gap-3">
        {warnings.map((warning) => {
          const copy = warningCopy(warning)
          return (
            <div className="flex gap-3 text-sm text-warning-foreground" key={`${warning.code}-${warning.scope}-${warning.key}`}>
              <CircleAlert className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <strong className="font-medium">{copy.title}</strong>
                  {warning.overridden && <Badge className="border-warning/40 bg-transparent text-warning" variant="outline">確認済み</Badge>}
                </div>
                <p className="mt-0.5 text-xs leading-relaxed text-warning-foreground/80">{copy.description}</p>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function HoldingsTable({ holdings, warnings }: { holdings: HoldingView[]; warnings: WarningView[] }) {
  const marketPriceAsOf = useMemo(() => {
    const values = Array.from(new Set(holdings.map((holding) => holding.market_price_as_of)))
    return values.length === 1 ? values[0] : null
  }, [holdings])

  return (
    <Card className="gap-0 overflow-hidden py-0 shadow-sm">
      <CardHeader className="flex flex-row items-start justify-between gap-4 border-b px-5 py-5 sm:px-6">
        <div>
          <CardTitle>保有銘柄</CardTitle>
          <CardDescription className="mt-1">{holdings.length} positions</CardDescription>
        </div>
        {marketPriceAsOf ? (
          <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <span>価格基準</span>
            <AsOfBadge compact value={marketPriceAsOf} />
          </div>
        ) : (
          <span className="text-xs text-muted-foreground">価格基準は銘柄ごとに異なります</span>
        )}
      </CardHeader>
      <Table>
        <TableHeader className="bg-muted/60">
          <TableRow className="hover:bg-transparent">
            <TableHead className="pl-5 sm:pl-6">銘柄</TableHead>
            <TableHead>sector</TableHead>
            <TableHead className="text-right">数量</TableHead>
            <TableHead className="text-right">取得 / 現在</TableHead>
            <TableHead className="text-right">評価額 / 損益</TableHead>
            <TableHead className="text-right">FV / 乖離</TableHead>
            <TableHead>判断</TableHead>
            <TableHead className="w-12 pr-5 text-right sm:pr-6"><span className="sr-only">チャート</span></TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {holdings.map((holding) => {
            const averageCostYen = holding.quantity === 0 ? null : holding.deployed_cost_yen / holding.quantity
            const pnlTone = holding.unrealized_pnl_yen > 0 ? 'text-positive' : holding.unrealized_pnl_yen < 0 ? 'text-destructive' : 'text-muted-foreground'
            return (
              <TableRow key={holding.ticker}>
                <TableCell className="pl-5 sm:pl-6">
                  <Link className="font-mono font-semibold text-foreground underline-offset-4 hover:underline" to={`/securities/${holding.ticker}`}>{holding.ticker}</Link>
                  <span className="mt-1 block max-w-44 truncate text-xs text-muted-foreground">{holding.company_name ?? '—'}</span>
                </TableCell>
                <TableCell className="text-muted-foreground">{holding.sector}</TableCell>
                <TableCell className="text-right font-mono tabular-nums">{holding.quantity.toLocaleString('ja-JP')}</TableCell>
                <TableCell className="text-right">
                  <span className="flex items-baseline justify-end gap-2 font-mono text-sm tabular-nums text-muted-foreground"><span className="text-[10px] font-medium">取得</span>{averageCostYen === null ? EMPTY : formatYen(averageCostYen)}</span>
                  <span className="mt-1 flex items-baseline justify-end gap-2 font-mono text-sm font-medium tabular-nums"><span className="text-[10px] font-medium text-muted-foreground">現在</span>{formatYen(Number(holding.market_price_yen))}</span>
                  {marketPriceAsOf === null && <AsOfBadge className="mt-1 justify-end" compact value={holding.market_price_as_of} />}
                </TableCell>
                <TableCell className="text-right">
                  <YenAmount className="text-sm font-medium" value={holding.market_value_yen} />
                  <span className={cn('mt-1 flex items-center justify-end gap-2 text-xs', pnlTone)}>
                    <YenAmount sign value={holding.unrealized_pnl_yen} />
                    <PctBadge className="text-xs" value={holding.unrealized_pnl_pct} />
                  </span>
                </TableCell>
                <TableCell className="text-right">
                  <YenAmount value={holding.fair_value_yen} />
                  <PctBadge className="mt-1 block text-xs" value={holding.fv_gap_pct} />
                </TableCell>
                <TableCell><Badge className="font-mono text-[10px] uppercase" variant="outline">{holding.recommendation ?? '—'}</Badge></TableCell>
                <TableCell className="pr-5 text-right sm:pr-6"><TradingViewButton ticker={holding.ticker} /></TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
      <PortfolioWarnings warnings={warnings} />
    </Card>
  )
}

const eventKindLabel: Record<UpcomingEventView['kind'], string> = {
  earnings: '決算',
  reservation_expiry: '予約期限',
  macro_valid_until: 'マクロ期限',
}

function eventCountdownLabel(daysUntil: number) {
  if (daysUntil <= 0) return '本日'
  if (daysUntil === 1) return '明日'
  return `あと ${daysUntil} 日`
}

function UpcomingEventsCard({ events }: { events: UpcomingEventView[] }) {
  return (
    <Card className="gap-0 overflow-hidden py-0 shadow-sm">
      <CardHeader className="flex flex-row items-start justify-between gap-4 border-b px-5 py-5 sm:px-6">
        <div className="flex items-center gap-2">
          <CalendarClock className="size-4 text-muted-foreground" aria-hidden="true" />
          <div><CardTitle>今後 14 日のイベント</CardTitle><CardDescription className="mt-1">決算・予約期限・マクロ期限</CardDescription></div>
        </div>
        <Badge variant="secondary">{events.length} 件</Badge>
      </CardHeader>
      {events.length === 0 ? (
        <CardContent className="py-8 text-center text-sm text-muted-foreground">今後 14 日のイベントはありません。</CardContent>
      ) : (
        <div className="divide-y">
          {events.map((event) => (
            <div className="grid grid-cols-[max-content_74px_minmax(0,1fr)] items-center gap-3 px-5 py-3 sm:px-6" key={`${event.kind}-${event.event_date}-${event.ticker ?? ''}`}>
              <time className="whitespace-nowrap font-mono text-sm tabular-nums" dateTime={event.event_date}>{formatJstDate(event.event_date)}</time>
              <Badge className="w-fit" variant={event.days_until <= 1 ? 'destructive' : 'outline'}>{eventCountdownLabel(event.days_until)}</Badge>
              <div className="flex min-w-0 items-center gap-2">
                <Badge className="shrink-0 font-mono text-[10px]" variant="secondary">{eventKindLabel[event.kind]}</Badge>
                {event.ticker ? (
                  <Link className="truncate font-medium underline-offset-4 hover:underline" to={`/securities/${event.ticker}`}>
                    <span className="font-mono">{event.ticker}</span>{event.label !== event.ticker && <span className="ml-2 text-muted-foreground">{event.label}</span>}
                  </Link>
                ) : (
                  <span className="truncate text-muted-foreground">{event.label}</span>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}

function planField(payload: Record<string, unknown>, key: string): unknown {
  const plan = payload.planned_limit
  if (typeof plan !== 'object' || plan === null) return undefined
  return (plan as Record<string, unknown>)[key]
}

const OPERATION_KIND_LABEL: Record<string, string> = {
  opportunity: '購入候補の選定',
  'pending-result': '注文結果の反映',
  'monthly-contribution': '入出金の反映',
  'earnings-material-event': '決算・重要イベント',
  'annual-outcome': '年次評価',
  improvement: '手法改善',
}

const STATUS_LABEL: Record<string, string> = {
  active: '進行中',
  completed: '完了',
  pending: '判断待ち',
  approved: '承認',
  deferred: '保留',
  rejected: '見送り',
  resolved: '評価済み',
  unresolved: '未確定',
}

function OperationCard({ operations }: { operations: OperationSessionView[] }) {
  return (
    <Card>
      <CardHeader><CardTitle>運用状況</CardTitle><CardDescription>候補選定・注文結果・保有見直しなど</CardDescription></CardHeader>
      <CardContent className="grid gap-3 text-sm">
        {operations.length === 0 ? <p className="text-muted-foreground">進行中または完了済みの運用はありません。</p> : operations.map((item) => (
          <div className="grid gap-1 border-b pb-3 last:border-0 last:pb-0" key={item.operation_id}>
            <div className="flex items-center justify-between gap-2">
              <span className="font-medium">{OPERATION_KIND_LABEL[item.session_kind] ?? item.session_kind}{item.ticker && <span className="ml-2 font-mono text-xs text-muted-foreground">{item.ticker}</span>}</span>
              <Badge variant="outline">{STATUS_LABEL[item.status] ?? item.status}</Badge>
            </div>
            <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
              <span className="truncate font-mono">{item.operation_id}</span>
              <AsOfBadge compact value={item.started_at} />
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  )
}

function ProposalCard({ proposals }: { proposals: ProposalView[] }) {
  return (
    <Card>
      <CardHeader><CardTitle>売買提案</CardTitle><CardDescription>指値・数量・期限と判断状況</CardDescription></CardHeader>
      <CardContent className="grid gap-3 text-sm">
        {proposals.length === 0 ? <p className="text-muted-foreground">売買提案はありません。</p> : proposals.map((item) => {
          const limit = planField(item.payload, 'limit_price_yen')
          const quantity = planField(item.payload, 'quantity')
          const expiresAt = planField(item.payload, 'expires_at')
          return (
            <div className="grid gap-1.5 border-b pb-3 last:border-0 last:pb-0" key={item.proposal_id}>
              <div className="flex items-center justify-between gap-2">
                <Link className="font-mono font-semibold underline-offset-4 hover:underline" to={`/securities/${item.ticker}`}>{item.ticker}</Link>
                <Badge variant="outline">{STATUS_LABEL[item.status] ?? item.status}</Badge>
              </div>
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
                <span>指値 <span className="font-mono tabular-nums text-foreground">{typeof limit === 'string' || typeof limit === 'number' ? formatYen(Number(limit)) : EMPTY}</span></span>
                <span>数量 <span className="font-mono tabular-nums text-foreground">{typeof quantity === 'number' ? `${quantity.toLocaleString('ja-JP')} 株` : EMPTY}</span></span>
                <span>期限 <span className="font-mono tabular-nums text-foreground">{typeof expiresAt === 'string' ? formatJstDate(expiresAt.slice(0, 10)) : EMPTY}</span></span>
              </div>
              <div className="flex flex-wrap items-center gap-x-3 text-[11px] text-muted-foreground">
                <span className="truncate font-mono" title={item.thesis_id}>{item.thesis_id}</span>
                <span>作成 {formatJstDate(item.created_at.slice(0, 10))}</span>
                {item.decided_at && <span>決定 {formatJstDate(item.decided_at.slice(0, 10))}</span>}
              </div>
              <details className="text-xs text-muted-foreground">
                <summary className="cursor-pointer select-none">payload 全体</summary>
                <pre className="mt-1 max-h-64 overflow-auto rounded-md bg-muted px-3 py-2 font-mono text-[11px] leading-relaxed">{JSON.stringify(item.payload, null, 2)}</pre>
              </details>
            </div>
          )
        })}
      </CardContent>
    </Card>
  )
}

function OutcomeCard({ outcomes }: { outcomes: PortfolioOutcomeView[] }) {
  return (
    <Card>
      <CardHeader><CardTitle>運用成績</CardTitle><CardDescription>ポートフォリオとベンチマークの期間比較</CardDescription></CardHeader>
      <CardContent className="grid gap-3 text-sm">
        {outcomes.length === 0 ? <p className="text-muted-foreground">運用成績はまだありません。</p> : outcomes.map((item) => (
          <div className="grid gap-1.5 border-b pb-3 last:border-0 last:pb-0" key={item.outcome_id}>
            <div className="flex items-center justify-between gap-2">
              <span className="font-medium">{item.horizon} · {item.period_end_date}</span>
              <Badge variant="outline">{STATUS_LABEL[item.status] ?? item.status}</Badge>
            </div>
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
              <span>ポート TWR <PctBadge className="text-xs" value={item.portfolio_twr_pct} /></span>
              <span>ベンチマーク <PctBadge className="text-xs" value={item.benchmark_cumulative_return_pct} /></span>
            </div>
            {item.reason && <p className="text-xs text-muted-foreground">{item.reason}</p>}
          </div>
        ))}
      </CardContent>
    </Card>
  )
}

function dashboardValuationAsOf(data: DashboardView): string | null {
  if (data.valuation_as_of) return data.valuation_as_of
  const holdingDates = data.holdings.map((holding) => holding.market_price_as_of)
  return holdingDates.sort().at(0) ?? data.ledger_as_of
}

function valuationIsStale(value: string | null): boolean {
  if (value === null) return false
  const formatter = new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Tokyo',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  })
  const today = formatter.format(new Date())
  const asOf = formatter.format(new Date(value))
  return Date.parse(`${today}T00:00:00Z`) - Date.parse(`${asOf}T00:00:00Z`) >= 7 * 86_400_000
}

export function DashboardPage() {
  const [data, setData] = useState<DashboardView | null>(null)
  const [operations, setOperations] = useState<OperationsView | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetchJson<DashboardView>('/api/dashboard').then(setData).catch((reason: unknown) => {
      setError(reason instanceof Error ? reason.message : 'Dashboard を読み込めませんでした')
    })
    fetchJson<OperationsView>('/api/operations').then(setOperations).catch((reason: unknown) => {
      setError(reason instanceof Error ? reason.message : '運用状態を読み込めませんでした')
    })
  }, [])

  if (error) return <PageState message={error} title="Dashboard read error" />
  if (!data) return <PageState message="資産状況を読み込んでいます…" title="Dashboard" />
  const valuationAsOf = dashboardValuationAsOf(data)

  return (
    <>
      <AppShell />
      <main className="mx-auto grid max-w-[1600px] gap-6 px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
        <header className="flex items-end justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">Dashboard</h1>
          </div>
          <div className="flex items-center gap-2">
            {valuationIsStale(valuationAsOf) && <StaleBadge />}
            <AsOfBadge value={valuationAsOf} />
          </div>
        </header>

        {data.ledger_error && (
          <Alert variant="destructive"><CircleAlert /><AlertTitle>Ledger error</AlertTitle><AlertDescription>{data.ledger_error}</AlertDescription></Alert>
        )}
        {data.research_load_errors.length > 0 && (
          <Alert variant="destructive"><CircleAlert /><AlertTitle>Research read error</AlertTitle><AlertDescription>{data.research_load_errors.join(' / ')}</AlertDescription></Alert>
        )}

        <PortfolioAllocationCard data={data} />

        <section className="grid gap-4 md:grid-cols-2" aria-label="次のアクション">
          <NextCard label="NEXT TASK" task={data.next_task} />
          <NextCard event label="NEXT EVENT" task={data.next_event} />
        </section>

        <UpcomingEventsCard events={data.upcoming_events} />

        {operations && (
          <section className="grid gap-4 lg:grid-cols-3" aria-label="運用・提案・評価">
            <OperationCard operations={operations.operations} />
            <ProposalCard proposals={operations.proposals} />
            <OutcomeCard outcomes={operations.outcomes} />
          </section>
        )}

        {!data.ledger_exists && !data.ledger_error ? (
          <Card className="border-dashed shadow-none"><CardContent className="py-8 text-center text-sm text-muted-foreground">portfolio ledger がありません。</CardContent></Card>
        ) : data.holdings.length > 0 ? (
          <HoldingsTable holdings={data.holdings} warnings={data.warnings} />
        ) : (
          <Card className="gap-0 overflow-hidden border-dashed py-0 shadow-none">
            <CardContent className="py-8 text-center text-sm text-muted-foreground">保有銘柄はありません。</CardContent>
            <PortfolioWarnings warnings={data.warnings} />
          </Card>
        )}

        {data.reservations.length > 0 && (
          <Card className="gap-0 overflow-hidden py-0 shadow-sm">
            <CardHeader className="border-b px-5 py-5 sm:px-6">
              <CardTitle>資金予約</CardTitle>
              <CardDescription>{data.reservations.length} reservations</CardDescription>
            </CardHeader>
            <Table>
              <TableHeader className="bg-muted/60">
                <TableRow className="hover:bg-transparent">
                  <TableHead className="pl-5 sm:pl-6">銘柄</TableHead>
                  <TableHead className="text-right">数量 / 指値</TableHead>
                  <TableHead className="text-right">予約額</TableHead>
                  <TableHead className="pr-5 text-right sm:pr-6">期限</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.reservations.map((reservation) => (
                  <TableRow key={reservation.reservation_id}>
                    <TableCell className="pl-5 sm:pl-6">
                      <Link className="font-mono font-semibold text-foreground underline-offset-4 hover:underline" to={`/securities/${reservation.ticker}`}>{reservation.ticker}</Link>
                    </TableCell>
                    <TableCell className="text-right font-mono tabular-nums">
                      {reservation.remaining_quantity.toLocaleString('ja-JP')} 株
                      <span className="ml-2 text-xs text-muted-foreground">× {formatYen(Number(reservation.price_guard_yen))}</span>
                    </TableCell>
                    <TableCell className="text-right"><YenAmount className="font-medium" value={reservation.reserved_yen} /></TableCell>
                    <TableCell className="pr-5 text-right sm:pr-6"><AsOfBadge compact value={reservation.expires_at} /></TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Card>
        )}

        <Card className="gap-0 overflow-hidden py-0 shadow-sm">
          <CardHeader className="flex flex-row items-start justify-between gap-4 border-b px-5 py-5 sm:px-6">
            <CardTitle>Open tasks</CardTitle>
            <Badge variant="secondary">{data.open_tasks.length} open</Badge>
          </CardHeader>
          {!data.tasks_exist ? (
            <CardContent className="py-8 text-center text-sm text-muted-foreground">task はまだ登録されていません</CardContent>
          ) : data.open_tasks.length === 0 ? (
            <CardContent className="py-8 text-center text-sm text-muted-foreground">open task はありません。</CardContent>
          ) : (
            <div className="divide-y">
              {data.open_tasks.map((task) => (
                <article className="grid items-center gap-2 px-5 py-4 sm:grid-cols-[160px_1fr_auto] sm:px-6" key={task.task_id}>
                  <time className="font-mono text-sm font-medium tabular-nums" dateTime={task.due_date}>{formatJstDate(task.due_date)}</time>
                  <strong className="text-sm font-medium">{task.title}</strong>
                  {task.overdue && <Badge variant="destructive">期限超過</Badge>}
                </article>
              ))}
            </div>
          )}
        </Card>
      </main>
    </>
  )
}
