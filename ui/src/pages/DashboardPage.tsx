import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { CircleAlert } from 'lucide-react'
import { Pie, PieChart } from 'recharts'

import { fetchJson } from '../api/client'
import type {
  DailyDeltaView,
  DeltaPool,
  DeltaUnavailable,
  DashboardView,
  HoldingView,
  OperationSessionView,
  OperationsView,
  PortfolioOutcomeView,
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
import { EMPTY, formatJstDate, formatJstDateShort, formatPct, formatYen, isOlderThanDays } from '../lib/format'
import { totalUnrealizedPnl } from '../lib/portfolio'
import { cn } from '../lib/utils'

// Why each section is on this page, carried behind its own ⓘ instead of a subtitle that
// repeats what the numbers already say.
const HINT = {
  allocation: '次の買いに動かせる資金がどれだけ残っているかと、これまでの判断が実際に効いているかを 1 か所で確かめる。配分は判断材料であり、比率を目安へ近づけること自体は目的ではない。',
  nextTask: '期限が最も近い未完了タスク。下の一覧の先頭と同じもので、開いて最初に目に入る位置に置いている。',
  events: '決算と予約期限は、保有の見直しと資金の解放が起きる日。判断より先に日付を押さえておくために置いている。',
  delta: '前回の機械実行と比べて何が動いたか。候補はその 2 run、マクロは reading の前営業日を比較端にする。候補プールへの出入り、機械 E[r] の変化、FV に達した保有、マクロ注記の点灯を観測として並べる。売買の指示ではなく、次にどこを見るかを決める材料。答えられなかった区分は明示するので、空欄と「計測できなかった」を混同しない。',
  operations: '判断は trigger ごとに 1 件の operation session として進み、active は常に最大 1 件。いま何が途中で、次にどこから再開するのかをここで確かめる。',
  holdings: '保有中の各銘柄の取得原価・現値・FV との乖離。売買判断そのものではなく、どの銘柄を次に見直すかを決めるための現状。',
  reservations: '発注済みで未約定の指値が押さえている現金。購入余力から差し引かれているので、次の提案の上限に効く。',
  tasks: '決算日や再評価日など、日付が来たら判断を始める合図。task が trigger 発火の正本で、期限超過は放置している判断を意味する。',
  outcomes: '税・費用込みの総合 return を、同じ期間の配当込み TOPIX と同じ basis で比べた結果。短期の数字で方針を変えるためではなく、見積りが実現と合っているかを年単位で確かめるために置いている。',
} as const

const allocationConfig = {
  holdings: { label: '保有株式', color: 'var(--chart-1)' },
  available: { label: '購入余力', color: 'var(--chart-2)' },
  reserved: { label: '予約', color: 'var(--chart-3)' },
} satisfies ChartConfig

function NextTaskCard({ task }: { task: TaskView | null }) {
  return (
    <SectionCard hint={HINT.nextTask} meta={task?.overdue === true && <Badge variant="destructive">期限超過</Badge>} padded title="次のタスク">
      {task ? (
        // Date and title side by side: one line of content fills the row rather than
        // stacking into a tall, mostly empty card.
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <time className="shrink-0 font-mono text-sm font-semibold tabular-nums" dateTime={task.due_date}>{formatJstDate(task.due_date)}</time>
          <p className="min-w-[16rem] flex-1 font-medium leading-snug">{task.title}</p>
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">未完了のタスクはありません。</p>
      )}
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
          <CardHeader className="flex flex-row items-center gap-1.5 px-0 pb-2">
            <CardTitle aria-level={2} className="text-base" role="heading">資産と損益</CardTitle>
            <InfoHint label="資産と損益">{HINT.allocation}</InfoHint>
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
      hint={HINT.holdings}
      meta={(
        <div className="flex flex-wrap items-baseline justify-end gap-x-2 text-xs">
          <Badge variant="secondary">{holdings.length} 銘柄</Badge>
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

function deltaCount(delta: DailyDeltaView) {
  return (
    delta.entered.length +
    delta.exited.length +
    delta.er_moves.length +
    delta.holdings.length +
    delta.macro_flags.length +
    delta.macro_extremes.length
  )
}

const deltaPoolLabel: Record<DeltaPool, string> = {
  longlist: '機械順位上位 20',
  recommendations: '機械推奨上位',
}

// The view names the section a store could not answer; the reader gets it in Japanese.
const deltaUnavailableLabel: Record<DeltaUnavailable, string> = {
  candidates: '候補（run なし）',
  candidates_estimate: '候補の E[r]（pool が見積りを持たない）',
  candidates_pool: '候補（選定出力なし）',
  candidates_previous_run: '候補（比較する前 run なし）',
  holdings: '保有（ledger なし）',
  holdings_fair_value: '保有の FV（thesis を読めない）',
  macro: 'マクロ（読み値なし）',
  market: '市場データ（store なし）',
}

function DeltaRow({ children, label }: { children: ReactNode; label: string }) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-5 py-3 sm:px-6">
      <Badge className="min-w-[6.5rem] shrink-0 justify-center font-mono text-[10px]" variant="secondary">{label}</Badge>
      <div className="flex min-w-[13rem] flex-1 flex-wrap items-center gap-x-3 gap-y-1">{children}</div>
    </div>
  )
}

function DeltaSecurity({ name, ticker }: { name: string | null; ticker: string }) {
  return (
    <Link className="min-w-0 truncate font-medium underline-offset-4 hover:underline" to={`/securities/${ticker}`}>
      <span className="font-mono">{ticker}</span>
      {name !== null && name !== ticker && <span className="ml-2 text-muted-foreground">{name}</span>}
    </Link>
  )
}

function DeltaDisclosedBadge({ disclosed }: { disclosed: boolean | null }) {
  if (disclosed === null) return <Badge variant="secondary">開示不明</Badge>
  return disclosed ? <Badge variant="outline">決算開示後</Badge> : null
}

function DailyDeltaCard({ delta, failed }: { delta: DailyDeltaView | null; failed: boolean }) {
  const total = delta === null ? 0 : deltaCount(delta)
  const description =
    delta !== null && delta.previous_asof !== null && delta.asof !== null
      ? `${formatJstDateShort(delta.previous_asof)} → ${formatJstDateShort(delta.asof)}${delta.pool === null ? '' : ` / ${deltaPoolLabel[delta.pool]}`}`
      : '前営業日との比較'
  return (
    <SectionCard
      description={description}
      hint={HINT.delta}
      meta={delta === null ? <Badge variant="secondary">未取得</Badge> : <Badge variant="secondary">{total} 件</Badge>}
      title="前回実行からの変化"
    >
      {delta === null ? (
        <p className="py-8 text-center text-sm text-muted-foreground">
          {failed ? '変化を読み込めませんでした。変化がないことを意味しません。' : '変化の観測がまだありません。'}
        </p>
      ) : (
        <div className="divide-y">
          {delta.unavailable.map((item) => (
            <DeltaRow key={`unavailable-${item}`} label="計測不能">
              <span className="text-sm text-muted-foreground">{deltaUnavailableLabel[item]}</span>
            </DeltaRow>
          ))}
          {delta.rules_changed && (
            <DeltaRow label="rules 改定">
              <span className="text-sm text-muted-foreground">2 run の screening rules が異なるため、候補の差分は市場の変化ではなく手法の変更。</span>
            </DeltaRow>
          )}
          {delta.entered.map((item) => (
            <DeltaRow key={`entered-${item.ticker}`} label="候補入り">
              <DeltaSecurity name={item.company_name} ticker={item.ticker} />
              {item.er_annual_pct !== null && <span className="font-mono text-sm tabular-nums">E[r] {formatPct(item.er_annual_pct)}</span>}
              <DeltaDisclosedBadge disclosed={item.disclosed_since_previous} />
            </DeltaRow>
          ))}
          {delta.exited.map((item) => (
            <DeltaRow key={`exited-${item.ticker}`} label="候補外れ">
              {/* A name that left the pool may have no detail view exported, so it is
                  shown as text rather than a link that would 404. */}
              <span className="min-w-0 truncate font-medium">
                <span className="font-mono">{item.ticker}</span>
                {item.company_name !== null && item.company_name !== item.ticker && (
                  <span className="ml-2 text-muted-foreground">{item.company_name}</span>
                )}
              </span>
              <DeltaDisclosedBadge disclosed={item.disclosed_since_previous} />
            </DeltaRow>
          ))}
          {delta.er_moves.map((item) => (
            <DeltaRow key={`move-${item.ticker}`} label="E[r] 変化">
              <DeltaSecurity name={item.company_name} ticker={item.ticker} />
              <span className="font-mono text-sm tabular-nums">
                {item.previous_er_annual_pct === null ? EMPTY : formatPct(item.previous_er_annual_pct)}
                {' → '}
                {item.er_annual_pct === null ? EMPTY : formatPct(item.er_annual_pct)}
              </span>
              <PctBadge value={item.change_pp} />
            </DeltaRow>
          ))}
          {delta.er_moves_total > delta.er_moves.length && (
            <DeltaRow label="E[r] 変化">
              <span className="text-sm text-muted-foreground">
                閾値を超えた変化は {delta.er_moves_total} 件で、上位 {delta.er_moves.length} 件を表示。
              </span>
            </DeltaRow>
          )}
          {delta.holdings.map((item) => (
            <DeltaRow key={`holding-${item.ticker}`} label="保有">
              <DeltaSecurity name={item.company_name} ticker={item.ticker} />
              {item.at_or_above_fair_value === true && <Badge variant="outline">FV 到達</Badge>}
              {item.change_since_previous_pct !== null && <PctBadge tone="pnl" value={item.change_since_previous_pct} />}
              {item.days_to_next_earnings !== null && <span className="text-sm text-muted-foreground">決算まで {item.days_to_next_earnings} 日</span>}
            </DeltaRow>
          ))}
          {delta.macro_flags.map((item) => (
            <DeltaRow key={`flag-${item.series_id}-${item.flag}`} label={item.state === 'raised' ? '注記点灯' : '注記解消'}>
              <span className="font-mono text-sm">{item.series_id}</span>
              <span className="text-sm text-muted-foreground">{item.flag}</span>
            </DeltaRow>
          ))}
          {delta.macro_extremes.map((item) => (
            <DeltaRow key={`extreme-${item.series_id}`} label="分布の端">
              <span className="font-mono text-sm">{item.series_id}</span>
              <span className="font-mono text-sm tabular-nums">
                z {item.previous_z_score === null ? EMPTY : item.previous_z_score.toFixed(2)} → {item.z_score.toFixed(2)}
              </span>
            </DeltaRow>
          ))}
          {delta.holdings_without_fair_value > 0 && (
            <DeltaRow label="FV 未記録">
              <span className="text-sm text-muted-foreground">保有 {delta.holdings_without_fair_value} 件は thesis の FV が無く、到達判定ができない。</span>
            </DeltaRow>
          )}
          {delta.holdings_without_price > 0 && (
            <DeltaRow label="価格なし">
              <span className="text-sm text-muted-foreground">保有 {delta.holdings_without_price} 件は FV があるのに現値が無く、比較できない。</span>
            </DeltaRow>
          )}
          {total === 0 &&
            delta.unavailable.length === 0 &&
            !delta.rules_changed &&
            delta.holdings_without_fair_value === 0 &&
            delta.holdings_without_price === 0 && (
              <p className="py-8 text-center text-sm text-muted-foreground">閾値に触れる変化はありません。</p>
            )}
        </div>
      )}
    </SectionCard>
  )
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
        <p className="py-8 text-center text-sm text-muted-foreground">今後 14 日のイベントはありません。</p>
      ) : (
        // The row wraps instead of scrolling sideways. `flex-1` alone would not wrap —
        // its basis is 0, so the security would shrink to an unreadable sliver rather
        // than reach a second line; the minimum width is what makes the wrap happen.
        <div className="divide-y">
          {events.map((event) => (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-5 py-3 sm:px-6" key={`${event.kind}-${event.event_date}-${event.ticker ?? ''}`}>
              {/* A minimum, not a fixed width: the dates line up across rows and a wider
                  one still renders in full. */}
              <time className="min-w-[5.5rem] shrink-0 font-mono text-sm tabular-nums" dateTime={event.event_date}>{formatJstDateShort(event.event_date)}</time>
              <Badge className="shrink-0" variant={event.days_until <= 1 ? 'destructive' : 'outline'}>{eventCountdownLabel(event.days_until)}</Badge>
              <div className="flex min-w-[13rem] flex-1 items-center gap-2">
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
  resolved: '評価済み',
  unresolved: '未確定',
}

function OperationCard({ operations }: { operations: OperationSessionView[] }) {
  return (
    <SectionCard
      hint={HINT.operations}
      meta={<Badge variant="secondary">{operations.length} 件</Badge>}
      title="運用状況"
    >
      {operations.length === 0 ? (
        <p className="py-8 text-center text-sm text-muted-foreground">進行中または完了済みの運用はありません。</p>
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

// Performance belongs on the dashboard because the portfolio has one owner asking one
// question — is this working — and there is nowhere else that question is answered. A
// dedicated screen is the natural home once an outcome carries more than a period
// comparison (annual review notes, per-holding attribution, calibration against the
// entry estimate); that surface does not exist yet.
function OutcomeCard({ outcomes }: { outcomes: PortfolioOutcomeView[] }) {
  return (
    <SectionCard
      description="ポートフォリオと配当込み TOPIX の期間比較"
      hint={HINT.outcomes}
      meta={<Badge variant="secondary">{outcomes.length} 件</Badge>}
      title="運用成績"
    >
      {outcomes.length === 0 ? (
        <p className="py-8 text-center text-sm text-muted-foreground">運用成績はまだありません。</p>
      ) : (
        <div className="divide-y">
          {outcomes.map((item) => (
            <div className="grid gap-1.5 px-5 py-3 sm:px-6" key={item.outcome_id}>
              <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
                <span className="font-medium">{item.horizon} · {item.period_end_date}</span>
                <Badge variant="outline">{STATUS_LABEL[item.status] ?? item.status}</Badge>
              </div>
              {/* The benchmark is a market index, but here it exists only to be read
                  against the portfolio's own return — the pair is one comparison, so both
                  wear the money colors rather than splitting across two systems. */}
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
                <span>ポート TWR <PctBadge className="text-xs" tone="pnl" value={item.portfolio_twr_pct} /></span>
                <span>ベンチマーク <PctBadge className="text-xs" tone="pnl" value={item.benchmark_cumulative_return_pct} /></span>
              </div>
              {item.reason && <p className="text-xs text-muted-foreground">{item.reason}</p>}
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

export function DashboardPage() {
  const [data, setData] = useState<DashboardView | null>(null)
  const [operations, setOperations] = useState<OperationsView | null>(null)
  const [delta, setDelta] = useState<DailyDeltaView | null>(null)
  const [deltaFailed, setDeltaFailed] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetchJson<DashboardView>('/api/dashboard').then(setData).catch((reason: unknown) => {
      setError(reason instanceof Error ? reason.message : 'Dashboard を読み込めませんでした')
    })
    fetchJson<OperationsView>('/api/operations').then(setOperations).catch((reason: unknown) => {
      setError(reason instanceof Error ? reason.message : '運用状態を読み込めませんでした')
    })
    // An extra read, not a required one: a serving generation deployed before this
    // view existed must still render the page, so its absence leaves the card empty
    // instead of failing the dashboard.
    fetchJson<DailyDeltaView>('/api/daily-delta')
      .then(setDelta)
      .catch(() => setDeltaFailed(true))
  }, [])

  if (error) return <PageState message={error} title="Dashboard read error" />
  if (!data) return <LoadingPage label="資産状況を読み込んでいます" />
  const valuationAsOf = dashboardValuationAsOf(data)

  return (
    <PageShell
      meta={(
        <div className="flex items-center gap-2">
          {valuationAsOf !== null && isOlderThanDays(valuationAsOf, 7) && <StaleBadge />}
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

      <DailyDeltaCard delta={delta} failed={deltaFailed} />

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

      {operations && <OperationCard operations={operations.operations} />}

      {operations && <OutcomeCard outcomes={operations.outcomes} />}

      <SectionCard hint={HINT.tasks} meta={<Badge variant="secondary">未完了 {data.open_tasks.length} 件</Badge>} title="タスク">
        {!data.tasks_exist ? (
          <p className="py-8 text-center text-sm text-muted-foreground">タスクはまだ登録されていません。</p>
        ) : data.open_tasks.length === 0 ? (
          <p className="py-8 text-center text-sm text-muted-foreground">未完了のタスクはありません。</p>
        ) : (
          // Same row shape as the event list: a date column wide enough to align, the
          // badge that qualifies it, then the title, which takes a second line on a
          // phone rather than being squeezed to a ribbon. A due date can be months out,
          // so this one keeps its year.
          <div className="divide-y">
            {data.open_tasks.map((task) => (
              <article className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-5 py-3 sm:px-6" key={task.task_id}>
                <time className="min-w-[10rem] shrink-0 font-mono text-sm tabular-nums" dateTime={task.due_date}>{formatJstDate(task.due_date)}</time>
                {task.overdue && <Badge className="shrink-0" variant="destructive">期限超過</Badge>}
                <span className="min-w-[16rem] flex-1 text-sm font-medium">{task.title}</span>
              </article>
            ))}
          </div>
        )}
      </SectionCard>
    </PageShell>
  )
}
