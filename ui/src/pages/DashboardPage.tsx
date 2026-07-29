import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { CircleAlert } from 'lucide-react'
import { Pie, PieChart } from 'recharts'

import { fetchJson } from '../api/client'
import type {
  DashboardView,
  HoldingView,
  OperationSessionView,
  OperationsView,
  TaskView,
  UpcomingEventView,
  WarningView,
} from '../api/types'
import { AsOfBadge } from '../components/AsOfBadge'
import { InfoHint } from '../components/InfoHint'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageShell } from '../components/PageShell'
import { PageState } from '../components/PageState'
import { PctBadge } from '../components/PctBadge'
import { SectionCard } from '../components/SectionCard'
import { StaleBadge } from '../components/StaleBadge'
import { TradingViewButton } from '../components/TradingViewButton'
import { YenAmount } from '../components/YenAmount'
import { Alert, AlertDescription, AlertTitle } from '../components/ui/alert'
import { Badge } from '../components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '../components/ui/card'
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from '../components/ui/chart'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../components/ui/table'
import { EMPTY, formatJstDate, formatJstDateShort, formatPct, formatYen } from '../lib/format'
import { totalUnrealizedPnl } from '../lib/portfolio'
import { cn } from '../lib/utils'

// Why each section is on this page, carried behind its own ⓘ instead of a subtitle that
// repeats what the numbers already say.
const HINT = {
  allocation: '次の買いに動かせる資金がどれだけ残っているかと、これまでの判断が実際に効いているかを 1 か所で確かめる。配分は判断材料であり、比率を目安へ近づけること自体は目的ではない。',
  nextTask: '期限が最も近い未完了タスク。下の一覧の先頭と同じもので、開いて最初に目に入る位置に置いている。',
  events: '決算と予約期限は、保有の見直しと資金の解放が起きる日。判断より先に日付を押さえておくために置いている。',
  operations: '判断は trigger ごとに 1 件の operation session として進み、active は常に最大 1 件。いま何が途中で、次にどこから再開するのかをここで確かめる。',
  holdings: '保有中の各銘柄の取得原価・現値・FV との乖離。売買判断そのものではなく、どの銘柄を次に見直すかを決めるための現状。',
  reservations: '発注済みで未約定の指値が押さえている現金。購入余力から差し引かれているので、次の提案の上限に効く。',
  tasks: '決算日や再評価日など、日付が来たら判断を始める合図。task が trigger 発火の正本で、期限超過は放置している判断を意味する。',
} as const

const allocationConfig = {
  holdings: { label: '保有株式', color: 'var(--chart-1)' },
  available: { label: '購入余力', color: 'var(--chart-2)' },
  reserved: { label: '予約', color: 'var(--chart-3)' },
} satisfies ChartConfig

function NextTaskCard({ task }: { task: TaskView | null }) {
  return (
    <SectionCard hint={HINT.nextTask} meta={task?.overdue === true && <Badge variant="destructive">期限超過</Badge>} title="次のタスク">
      <div className="px-5 py-4 sm:px-6">
        {task ? (
          // Date and title side by side: one line of content fills the row rather than
          // stacking into a tall, mostly empty card.
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <time className="font-mono text-sm font-semibold tabular-nums" dateTime={task.due_date ?? undefined}>{formatJstDate(task.due_date)}</time>
            <p className="min-w-0 flex-1 font-medium leading-snug">{task.title}</p>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">未完了のタスクはありません。</p>
        )}
      </div>
    </SectionCard>
  )
}

function PortfolioAllocationCard({ data }: { data: DashboardView }) {
  const allocation = [
    { key: 'holdings', name: '保有株式', value: Math.max(data.holdings_market_value_yen ?? 0, 0), fill: 'var(--color-holdings)' },
    { key: 'available', name: '購入余力', value: Math.max(data.available_cash_yen ?? 0, 0), fill: 'var(--color-available)' },
    { key: 'reserved', name: '予約', value: Math.max(data.reserved_cash_yen ?? 0, 0), fill: 'var(--color-reserved)' },
  ]
  const hasAllocation = allocation.some((item) => item.value > 0)
  // What the capital is split into, and what it has earned so far: the card answers both,
  // because a total says nothing about whether holding it has been worth anything.
  const pnl = totalUnrealizedPnl(data.holdings)
  const hasPnl = data.holdings.length > 0

  return (
    <Card className="overflow-hidden py-0 shadow-sm">
      <div className="grid min-w-0 lg:grid-cols-[minmax(320px,0.8fr)_1.2fr]">
        <div className="flex min-w-0 flex-col border-b p-5 lg:border-r lg:border-b-0 sm:p-6">
          <CardHeader className="px-0 pb-2">
            <CardTitle className="flex items-center gap-1.5 text-base">資産と損益<InfoHint label="資産と損益">{HINT.allocation}</InfoHint></CardTitle>
          </CardHeader>
          <div className="relative mx-auto min-h-[230px] w-full max-w-[360px] flex-1">
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
                {/* Amount only. A percentage here would sit under the total and read as a
                    share of it, when it is measured against deployed cost — the row list
                    states that denominator alongside its own figure. */}
                {hasPnl && <YenAmount className="mt-1.5 text-xs font-medium" sign tone="pnl" value={pnl.yen} />}
              </div>
            )}
          </div>
        </div>

        <div className="grid min-w-0 content-center divide-y">
          {[
            { label: '総資産', value: data.total_capital_yen, detail: `${data.holdings.length} 銘柄を保有`, color: 'bg-foreground', tone: 'plain' as const, sign: false },
            { label: '保有株式', value: data.holdings_market_value_yen, detail: data.deployed_pct === null ? '評価額' : `総資産の ${formatPct(data.deployed_pct)}`, color: 'bg-chart-1', tone: 'plain' as const, sign: false },
            { label: '購入余力', value: data.available_cash_yen, detail: data.cash_pct === null ? '利用可能な現金' : `総資産の ${formatPct(data.cash_pct)}`, color: 'bg-chart-2', tone: 'plain' as const, sign: false },
            { label: '予約', value: data.reserved_cash_yen, detail: data.reserved_pct === null ? '確保済みの現金' : `総資産の ${formatPct(data.reserved_pct)}`, color: 'bg-chart-3', tone: 'plain' as const, sign: false },
            { label: '評価損益', value: hasPnl ? pnl.yen : null, detail: pnl.pct === null ? '取得原価に対する損益' : `取得原価比 ${formatPct(pnl.pct, { sign: true })}`, color: null, tone: 'pnl' as const, sign: true },
          ].map((metric) => (
            <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3 px-5 py-5 sm:px-6" key={metric.label}>
              <div className="flex min-w-0 items-center gap-3">
                {/* Only the ring's own slices get a swatch. P&L is not one of them, so it
                    keeps the column's alignment without claiming a fourth segment. */}
                <span className={cn('size-2.5 shrink-0 rounded-sm', metric.color)} aria-hidden="true" />
                <div>
                  <p className="text-sm font-medium">{metric.label}</p>
                  <p className="mt-0.5 text-xs text-muted-foreground">{metric.detail}</p>
                </div>
              </div>
              <YenAmount className="shrink-0 text-sm font-semibold tracking-tight sm:text-base" sign={metric.sign} tone={metric.tone} value={metric.value} />
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
            <div className="flex gap-3 text-sm text-warning-ink" key={`${warning.code}-${warning.scope}-${warning.key}`}>
              <CircleAlert className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <strong className="font-medium">{copy.title}</strong>
                  {warning.overridden && <Badge className="border-warning-ink/40 bg-transparent text-warning-ink" variant="outline">確認済み</Badge>}
                </div>
                <p className="mt-0.5 text-xs leading-relaxed text-warning-ink/80">{copy.description}</p>
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
  const unrealizedPnl = useMemo(() => totalUnrealizedPnl(holdings), [holdings])

  return (
    // The page header already states the valuation basis. It is repeated here only when
    // the holdings disagree on it, and then per row rather than as one date.
    <SectionCard
      description={`${holdings.length} 銘柄`}
      hint={HINT.holdings}
      meta={(
        <div className="flex flex-wrap items-baseline justify-end gap-x-2 text-xs">
          <span className="text-muted-foreground">評価損益 合計</span>
          <YenAmount className="font-semibold" sign tone="pnl" value={unrealizedPnl.yen} />
          <PctBadge className="text-xs" tone="pnl" value={unrealizedPnl.pct} />
        </div>
      )}
      title="保有銘柄"
    >
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
                  <span className="mt-1 flex items-center justify-end gap-2 text-xs">
                    <YenAmount sign tone="pnl" value={holding.unrealized_pnl_yen} />
                    <PctBadge className="text-xs" tone="pnl" value={holding.unrealized_pnl_pct} />
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
    </SectionCard>
  )
}

const eventKindLabel: Record<UpcomingEventView['kind'], string> = {
  earnings: '決算',
  reservation_expiry: '予約期限',
}

function eventCountdownLabel(daysUntil: number) {
  if (daysUntil <= 0) return '本日'
  if (daysUntil === 1) return '明日'
  return `あと ${daysUntil} 日`
}

function UpcomingEventsCard({ events }: { events: UpcomingEventView[] }) {
  return (
    <SectionCard
      description="決算・予約期限"
      hint={HINT.events}
      meta={<Badge variant="secondary">{events.length} 件</Badge>}
      title="今後 14 日のイベント"
    >
      {events.length === 0 ? (
        <CardContent className="py-8 text-center text-sm text-muted-foreground">今後 14 日のイベントはありません。</CardContent>
      ) : (
        // No minimum width anywhere: the window is 14 days, so a short date plus the
        // countdown beside it fits a phone, and the security wraps to a second line
        // rather than pushing the row into a sideways scroll.
        <div className="divide-y">
          {events.map((event) => (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-5 py-3 sm:px-6" key={`${event.kind}-${event.event_date}-${event.ticker ?? ''}`}>
              {/* A minimum, not a fixed width: the dates line up across rows and a wider
                  one still renders in full. */}
              <time className="min-w-[5.5rem] shrink-0 font-mono text-sm tabular-nums" dateTime={event.event_date}>{formatJstDateShort(event.event_date)}</time>
              <Badge className="shrink-0" variant={event.days_until <= 1 ? 'destructive' : 'outline'}>{eventCountdownLabel(event.days_until)}</Badge>
              <div className="flex min-w-0 flex-1 items-center gap-2">
                <Badge className="shrink-0 font-mono text-[10px]" variant="secondary">{eventKindLabel[event.kind]}</Badge>
                {event.ticker ? (
                  <Link className="min-w-0 truncate font-medium underline-offset-4 hover:underline" to={`/securities/${event.ticker}`}>
                    <span className="font-mono">{event.ticker}</span>{event.label !== event.ticker && <span className="ml-2 text-muted-foreground">{event.label}</span>}
                  </Link>
                ) : (
                  <span className="min-w-0 truncate text-muted-foreground">{event.label}</span>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </SectionCard>
  )
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
}

function OperationCard({ operations }: { operations: OperationSessionView[] }) {
  return (
    <SectionCard
      description="1 つの trigger を 1 件の session で進める"
      hint={HINT.operations}
      meta={<Badge variant="secondary">{operations.length} 件</Badge>}
      title="運用状況"
    >
      {operations.length === 0 ? (
        <CardContent className="py-8 text-center text-sm text-muted-foreground">進行中または完了済みの運用はありません。</CardContent>
      ) : (
        <div className="divide-y">
          {operations.map((item) => (
            <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 px-5 py-3 sm:px-6" key={item.operation_id}>
              <div className="min-w-0">
                <p className="font-medium">
                  {OPERATION_KIND_LABEL[item.session_kind] ?? item.session_kind}
                  {item.ticker && <span className="ml-2 font-mono text-xs text-muted-foreground">{item.ticker}</span>}
                </p>
                <p className="truncate font-mono text-xs text-muted-foreground">{item.operation_id}</p>
              </div>
              <div className="flex shrink-0 items-center gap-3">
                <AsOfBadge compact value={item.started_at} />
                <Badge variant="outline">{STATUS_LABEL[item.status] ?? item.status}</Badge>
              </div>
            </div>
          ))}
        </div>
      )}
    </SectionCard>
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
  if (!data) return <LoadingPage label="資産状況を読み込んでいます" />
  const valuationAsOf = dashboardValuationAsOf(data)

  return (
    <PageShell
      meta={(
        <div className="flex items-center gap-2">
          {valuationIsStale(valuationAsOf) && <StaleBadge />}
          <AsOfBadge value={valuationAsOf} />
        </div>
      )}
      title="Dashboard"
    >
      {data.ledger_error && (
        <Alert variant="destructive"><CircleAlert /><AlertTitle>Ledger error</AlertTitle><AlertDescription>{data.ledger_error}</AlertDescription></Alert>
      )}
      {data.research_load_errors.length > 0 && (
        <Alert variant="destructive"><CircleAlert /><AlertTitle>Research read error</AlertTitle><AlertDescription>{data.research_load_errors.join(' / ')}</AlertDescription></Alert>
      )}

      <PortfolioAllocationCard data={data} />

      <NextTaskCard task={data.next_task} />

      <UpcomingEventsCard events={data.upcoming_events} />

      {operations && <OperationCard operations={operations.operations} />}

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
        <SectionCard hint={HINT.reservations} meta={<Badge variant="secondary">{data.reservations.length} 件</Badge>} title="資金予約">
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
        </SectionCard>
      )}

      <SectionCard hint={HINT.tasks} meta={<Badge variant="secondary">未完了 {data.open_tasks.length} 件</Badge>} title="タスク">
        {!data.tasks_exist ? (
          <CardContent className="py-8 text-center text-sm text-muted-foreground">タスクはまだ登録されていません。</CardContent>
        ) : data.open_tasks.length === 0 ? (
          <CardContent className="py-8 text-center text-sm text-muted-foreground">未完了のタスクはありません。</CardContent>
        ) : (
          // Same row shape as the event list: a date column wide enough to align, the
          // badge that qualifies it, then the text. A due date can be months out, so
          // this one keeps its year.
          <div className="divide-y">
            {data.open_tasks.map((task) => (
              <article className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-5 py-3 sm:px-6" key={task.task_id}>
                <time className="min-w-[10rem] shrink-0 font-mono text-sm tabular-nums" dateTime={task.due_date}>{formatJstDate(task.due_date)}</time>
                {task.overdue && <Badge className="shrink-0" variant="destructive">期限超過</Badge>}
                <span className="min-w-0 flex-1 text-sm font-medium">{task.title}</span>
              </article>
            ))}
          </div>
        )}
      </SectionCard>
    </PageShell>
  )
}
