import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { Link } from 'react-router'
import { CircleAlert } from 'lucide-react'
import { Pie, PieChart } from 'recharts'

import { fetchJson } from '../api/client'
import type {
  DailyDeltaView,
  DeltaUnavailable,
  DashboardView,
  HoldingView,
  OperationsView,
  PortfolioOutcomeView,
  WarningView,
} from '../api/types'
import { AsOfBadge } from '../components/AsOfBadge'
import { InfoHint } from '../components/InfoHint'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageShell } from '../components/PageShell'
import { PageState } from '../components/PageState'
import { PctBadge } from '../components/PctBadge'
import { SectionCard } from '../components/SectionCard'
import { TradingViewButton } from '../components/TradingViewButton'
import { YenAmount } from '../components/YenAmount'
import { Alert, AlertDescription, AlertTitle } from '../components/ui/alert'
import { Badge } from '../components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '../components/ui/card'
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from '../components/ui/chart'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../components/ui/table'
import { EMPTY, formatJstDateShort, formatPct, formatYen } from '../lib/format'
import { totalUnrealizedPnl } from '../lib/portfolio'
import { cn } from '../lib/utils'

// Why each section is on this page, carried behind its own ⓘ instead of a subtitle that
// repeats what the numbers already say.
const HINT = {
  allocation: '次の買いに動かせる資金がどれだけ残っているかと、これまでの判断が実際に効いているかを 1 か所で確かめる。配分は判断材料であり、比率を目安へ近づけること自体は目的ではない。',
  delta: '前回の機械実行と比べた候補プールへの出入り、機械 E[r] の変化、FV に達した保有を並べる。Macro の現在局面と指標は専用画面で読む。売買の指示ではなく、次にどこを見るかを決める材料。答えられなかった区分は明示するので、空欄と「計測できなかった」を混同しない。',
  holdings: '保有中の各銘柄の取得原価・現値・FV との乖離。売買判断そのものではなく、どの銘柄を次に見直すかを決めるための現状。',
  reservations: '発注済みで未約定の指値が押さえている現金。購入余力から差し引かれているので、次の提案の上限に効く。',
  outcomes: '税・費用込みの総合 return を、同じ期間の配当込み TOPIX と同じ basis で比べた結果。短期の数字で方針を変えるためではなく、見積りが実現と合っているかを年単位で確かめるために置いている。',
} as const

const allocationConfig = {
  holdings: { label: '保有株式', color: 'var(--chart-1)' },
  available: { label: '購入余力', color: 'var(--chart-2)' },
  reserved: { label: '予約', color: 'var(--chart-3)' },
} satisfies ChartConfig

export function PortfolioAllocationCard({ data }: { data: DashboardView }) {
  const {
    total_capital_yen: total,
    holdings_market_value_yen: holdingsValue,
    available_cash_yen: available,
    reserved_cash_yen: reserved,
  } = data
  const allocation = total !== null && holdingsValue !== null && available !== null && reserved !== null
    ? [
        { key: 'holdings', name: '保有株式', value: holdingsValue, fill: 'var(--color-holdings)' },
        { key: 'available', name: '購入余力', value: available, fill: 'var(--color-available)' },
        { key: 'reserved', name: '予約', value: reserved, fill: 'var(--color-reserved)' },
      ]
    : []
  const hasAllocation = allocation.some((item) => item.value > 0)
  const unvaluedHoldings = data.holdings.filter((holding) => holding.market_value_yen === null)
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
          {data.valuation_as_of !== null && (
            <p className="text-xs text-muted-foreground">株価基準 {data.valuation_as_of.slice(0, 10)}</p>
          )}
          {unvaluedHoldings.length > 0 && (
            <p className="mt-2 text-sm leading-relaxed text-muted-foreground" role="status">
              一部の保有株式が未評価のため、総資産・評価損益・資産配分を表示できません。未評価: {unvaluedHoldings.map((holding) => holding.ticker).join('、')}
            </p>
          )}
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
              {metric.value === null && unvaluedHoldings.length > 0 && ['総資産', '保有株式', '評価損益'].includes(metric.label)
                ? <span className="shrink-0 text-sm font-semibold text-muted-foreground">未評価</span>
                : <YenAmount className="shrink-0 text-sm font-semibold tracking-tight sm:text-base" sign={metric.sign} tone={metric.tone} value={metric.value} />}
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

export function HoldingsTable({ holdings, warnings }: { holdings: HoldingView[]; warnings: WarningView[] }) {
  const marketPriceAsOf = useMemo(() => {
    const values = Array.from(new Set(holdings.map((holding) => holding.market_price_as_of)))
    return values.length === 1 ? values[0] : null
  }, [holdings])
  const unrealizedPnl = useMemo(() => totalUnrealizedPnl(holdings), [holdings])

  return (
    // The asset card states the shared valuation basis. Per-row dates show a mixed basis.
    <SectionCard
      hint={HINT.holdings}
      meta={(
        <div className="flex flex-wrap items-baseline justify-end gap-x-2 text-xs">
          <Badge variant="secondary">{holdings.length} 銘柄</Badge>
          <span className="text-muted-foreground">評価損益 合計</span>
          {unrealizedPnl.yen === null && holdings.some((holding) => holding.market_value_yen === null)
            ? <span className="font-semibold text-muted-foreground">未評価</span>
            : <YenAmount className="font-semibold" sign tone="pnl" value={unrealizedPnl.yen} />}
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
            <TableHead className="text-right">原評価のPmax / 差</TableHead>
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
                  <span className="mt-1 flex items-baseline justify-end gap-2 font-mono text-sm font-medium tabular-nums"><span className="text-[10px] font-medium text-muted-foreground">現在</span>{holding.market_price_yen === null ? '未評価' : formatYen(Number(holding.market_price_yen))}</span>
                  {marketPriceAsOf === null && holding.market_price_as_of !== null && (
                    <span className="mt-1 flex items-center justify-end gap-1 text-xs text-muted-foreground">株価基準 <time dateTime={holding.market_price_as_of}>{holding.market_price_as_of.slice(0, 10)}</time></span>
                  )}
                </TableCell>
                <TableCell className="text-right">
                  {holding.market_value_yen === null
                    ? <span className="text-sm font-medium text-muted-foreground">未評価</span>
                    : <YenAmount className="text-sm font-medium" value={holding.market_value_yen} />}
                  <span className="mt-1 flex items-center justify-end gap-2 text-xs">
                    <YenAmount sign tone="pnl" value={holding.unrealized_pnl_yen} />
                    <PctBadge className="text-xs" tone="pnl" value={holding.unrealized_pnl_pct} />
                  </span>
                </TableCell>
                <TableCell className="text-right">
                  <YenAmount value={holding.pmax_raw_yen} />
                  <PctBadge className="mt-1 block text-xs" value={holding.pmax_gap_pct} />
                </TableCell>
                <TableCell><Badge className="font-mono text-[10px] uppercase" variant="outline">{holding.disposition ?? '—'}</Badge></TableCell>
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

function deltaCount(delta: DailyDeltaView) {
  return (
    delta.entered.length +
    delta.exited.length +
    delta.er_moves_total +
    delta.holdings.length
  )
}

// The view names the section a store could not answer; the reader gets it in Japanese.
const deltaUnavailableLabel: Record<DeltaUnavailable, string> = {
  screening_run: 'Screening Run（run なし）',
  previous_screening_run: 'Screening Run（比較する前 run なし）',
  review_set: 'Review Set（選定出力または比較に必要な手法情報なし）',
  review_set_estimate: 'Review Set の E[r]（見積り欠損またはモデルを比較できない）',
  holdings: '保有（ledger なし）',
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

function DeltaEntries({ children, count, label }: { children: ReactNode; count: number; label: string }) {
  if (count === 0) return null
  return (
    <details open={count <= 10}>
      <summary className="cursor-pointer px-5 py-3 text-sm sm:px-6">{label} 全{count}件（銘柄コード順・展開して全件表示）</summary>
      <div className="divide-y">{children}</div>
    </details>
  )
}

export function DailyDeltaCard({ delta, failed }: { delta: DailyDeltaView | null; failed: boolean }) {
  const total = delta === null ? 0 : deltaCount(delta)
  const description =
    delta !== null && delta.previous_as_of !== null && delta.as_of !== null
      ? `${formatJstDateShort(delta.previous_as_of)} → ${formatJstDateShort(delta.as_of)}`
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
          {delta.method_changed && (
            <DeltaRow label="手法変更">
              <span className="text-sm text-muted-foreground">screening または Candidate Discovery の手法が異なるため、候補差分を市場の変化として比較しない。</span>
            </DeltaRow>
          )}
          <DeltaEntries count={delta.entered.length} label="候補入り">
          {delta.entered.map((item) => (
            <DeltaRow key={`entered-${item.ticker}`} label="候補入り">
              <DeltaSecurity name={item.company_name} ticker={item.ticker} />
              {item.er_annual_pct !== null && <span className="font-mono text-sm tabular-nums">E[r] {formatPct(item.er_annual_pct)}</span>}
              <DeltaDisclosedBadge disclosed={item.disclosed_since_previous} />
            </DeltaRow>
          ))}
          </DeltaEntries>
          <DeltaEntries count={delta.exited.length} label="候補外れ">
          {delta.exited.map((item) => (
            <DeltaRow key={`exited-${item.ticker}`} label="候補外れ">
              {/* A name that left the Review Set may have no detail view exported, so it is
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
          </DeltaEntries>
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
              {item.change_since_previous_pct !== null && <PctBadge tone="pnl" value={item.change_since_previous_pct} />}
              {item.days_to_next_earnings !== null && <span className="text-sm text-muted-foreground">決算まで {item.days_to_next_earnings} 日</span>}
            </DeltaRow>
          ))}
          {delta.holdings_without_price > 0 && (
            <DeltaRow label="価格なし">
              <span className="text-sm text-muted-foreground">保有 {delta.holdings_without_price} 件は現値が無く、価格変化を確認できない。</span>
            </DeltaRow>
          )}
          {total === 0 &&
            delta.unavailable.length === 0 &&
            !delta.method_changed &&
            delta.holdings_without_price === 0 && (
              <p className="py-8 text-center text-sm text-muted-foreground">閾値に触れる変化はありません。</p>
            )}
          <div className="px-5 py-3 sm:px-6">
            <Link className="text-sm font-medium underline underline-offset-4" to="/macro">現在のマクロ局面を見る</Link>
          </div>
        </div>
      )}
    </SectionCard>
  )
}

const STATUS_LABEL: Record<string, string> = {
  active: '進行中',
  completed: '完了',
  resolved: '評価済み',
  unresolved: '未確定',
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
  return (
    <PageShell title="Dashboard">
      {data.ledger_error && (
        <Alert variant="destructive"><CircleAlert /><AlertTitle>Ledger error</AlertTitle><AlertDescription>{data.ledger_error}</AlertDescription></Alert>
      )}
      {data.research_load_errors.length > 0 && (
        <Alert variant="destructive"><CircleAlert /><AlertTitle>Research read error</AlertTitle><AlertDescription>{data.research_load_errors.join(' / ')}</AlertDescription></Alert>
      )}

      <PortfolioAllocationCard data={data} />

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

      {operations && <OutcomeCard outcomes={operations.outcomes} />}
    </PageShell>
  )
}
