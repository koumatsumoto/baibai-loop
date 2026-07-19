import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { CalendarCheck2, CalendarDays, ChartNoAxesCombined, CircleAlert } from 'lucide-react'
import { Pie, PieChart } from 'recharts'

import { fetchJson } from '../api/client'
import type { DashboardView, HoldingView, TaskView, WarningView } from '../api/types'
import { AppShell } from '../components/AppShell'
import { AsOfBadge } from '../components/AsOfBadge'
import { PctBadge } from '../components/PctBadge'
import { YenAmount } from '../components/YenAmount'
import { Alert, AlertDescription, AlertTitle } from '../components/ui/alert'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/card'
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from '../components/ui/chart'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../components/ui/table'
import { Tooltip, TooltipContent, TooltipTrigger } from '../components/ui/tooltip'
import { cn } from '../lib/utils'
import { tradingViewChartUrl } from '../lib/trading-view'

const allocationConfig = {
  holdings: { label: '保有株式', color: 'var(--chart-1)' },
  available: { label: '購入余力', color: 'var(--chart-2)' },
  reserved: { label: '予約', color: 'var(--chart-3)' },
} satisfies ChartConfig

const yenFormatter = new Intl.NumberFormat('ja-JP', {
  style: 'currency',
  currency: 'JPY',
  maximumFractionDigits: 0,
})

function formatDate(value: string | null) {
  if (value === null) return '日時なし'
  return new Intl.DateTimeFormat('ja-JP', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    weekday: 'short',
  }).format(new Date(`${value}T00:00:00+09:00`))
}

function PageState({ title, message }: { title: string; message: string }) {
  return (
    <>
      <AppShell />
      <main className="mx-auto grid min-h-[60vh] max-w-5xl place-items-center px-6 text-center">
        <div>
          <p className="text-sm font-medium text-muted-foreground">{title}</p>
          <h1 className="mt-2 text-2xl font-semibold tracking-tight">{message}</h1>
        </div>
      </main>
    </>
  )
}

function TradingViewButton({ ticker }: { ticker: string }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button asChild size="icon-sm" variant="ghost">
          <a
            aria-label={`${ticker} の TradingView チャートを開く`}
            href={tradingViewChartUrl(ticker)}
            rel="noopener noreferrer"
            target="_blank"
          >
            <ChartNoAxesCombined aria-hidden="true" />
          </a>
        </Button>
      </TooltipTrigger>
      <TooltipContent>TradingView でチャートを開く</TooltipContent>
    </Tooltip>
  )
}

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
            <time className="w-fit rounded-md bg-muted px-2.5 py-1.5 font-mono text-sm font-semibold tabular-nums text-foreground ring-1 ring-foreground/10" dateTime={date ?? undefined}>{formatDate(date)}</time>
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
                        <span className="font-mono font-medium tabular-nums">{yenFormatter.format(Number(value))}</span>
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
            { label: '保有株式', value: data.holdings_market_value_yen, detail: data.deployed_pct === null ? '評価額' : `総資産の ${data.deployed_pct.toFixed(1)}%`, color: 'bg-chart-1' },
            { label: '購入余力', value: data.available_cash_yen, detail: data.reserved_cash_yen === null ? '利用可能な現金' : `予約 ${yenFormatter.format(data.reserved_cash_yen)}`, color: 'bg-chart-2' },
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
  const actual = `${warning.actual_pct.toFixed(2)}%`
  const threshold = `${warning.warning_pct.toFixed(2)}%`
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
    <div className="border-t bg-amber-50/60 px-5 py-4 sm:px-6" aria-label="ポートフォリオ確認事項">
      <div className="grid gap-3">
        {warnings.map((warning) => {
          const copy = warningCopy(warning)
          return (
            <div className="flex gap-3 text-sm text-amber-950" key={`${warning.code}-${warning.scope}-${warning.key}`}>
              <CircleAlert className="mt-0.5 size-4 shrink-0 text-amber-600" aria-hidden="true" />
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <strong className="font-medium">{copy.title}</strong>
                  {warning.overridden && <Badge className="border-amber-300 bg-transparent text-amber-800" variant="outline">確認済み</Badge>}
                </div>
                <p className="mt-0.5 text-xs leading-relaxed text-amber-900/80">{copy.description}</p>
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
                  <span className="flex items-baseline justify-end gap-2 font-mono text-sm tabular-nums text-muted-foreground"><span className="text-[10px] font-medium">取得</span>{averageCostYen === null ? '—' : yenFormatter.format(averageCostYen)}</span>
                  <span className="mt-1 flex items-baseline justify-end gap-2 font-mono text-sm font-medium tabular-nums"><span className="text-[10px] font-medium text-muted-foreground">現在</span>{yenFormatter.format(Number(holding.market_price_yen))}</span>
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

export function DashboardPage() {
  const [data, setData] = useState<DashboardView | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetchJson<DashboardView>('/api/dashboard').then(setData).catch((reason: unknown) => {
      setError(reason instanceof Error ? reason.message : 'Dashboard を読み込めませんでした')
    })
  }, [])

  if (error) return <PageState message={error} title="Dashboard read error" />
  if (!data) return <PageState message="資産状況を読み込んでいます…" title="Dashboard" />

  return (
    <>
      <AppShell />
      <main className="mx-auto grid max-w-[1600px] gap-6 px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
        <header className="flex items-end justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">Dashboard</h1>
            <p className="mt-1 text-sm text-muted-foreground">資産状況と次のアクション</p>
          </div>
          <AsOfBadge stale={data.ledger_stale} value={data.ledger_as_of} />
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
                    <TableCell className="pl-5 font-mono font-semibold sm:pl-6">{reservation.ticker}</TableCell>
                    <TableCell className="text-right font-mono tabular-nums">
                      {reservation.remaining_quantity.toLocaleString('ja-JP')} 株
                      <span className="ml-2 text-xs text-muted-foreground">× {yenFormatter.format(Number(reservation.price_guard_yen))}</span>
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
            <div><CardTitle>Open tasks</CardTitle><CardDescription className="mt-1">運用上の次アクション</CardDescription></div>
            <Badge variant="secondary">{data.open_tasks.length} open</Badge>
          </CardHeader>
          {!data.tasks_exist ? (
            <CardContent className="py-8 text-center text-sm text-muted-foreground">task record 未作成（records/05-task/tasks.yaml）</CardContent>
          ) : data.open_tasks.length === 0 ? (
            <CardContent className="py-8 text-center text-sm text-muted-foreground">open task はありません。</CardContent>
          ) : (
            <div className="divide-y">
              {data.open_tasks.map((task) => (
                <article className="grid items-center gap-2 px-5 py-4 sm:grid-cols-[160px_1fr_auto] sm:px-6" key={task.task_id}>
                  <time className="font-mono text-sm font-medium tabular-nums" dateTime={task.due_date}>{formatDate(task.due_date)}</time>
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
