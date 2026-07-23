import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowDown, ArrowUp, ArrowUpDown, Search } from 'lucide-react'

import { fetchJson } from '../api/client'
import type { CandidateRowView, PortfolioState, ScreeningView } from '../api/types'
import { AppShell } from '../components/AppShell'
import { PageState } from '../components/PageState'
import { PctBadge } from '../components/PctBadge'
import { StaleBadge } from '../components/StaleBadge'
import { TradingViewButton } from '../components/TradingViewButton'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/card'
import { Checkbox } from '../components/ui/checkbox'
import { Input } from '../components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../components/ui/table'
import { EMPTY, formatJstDateTime, formatNumber } from '../lib/format'
import { LABEL } from '../lib/labels'
import { cn } from '../lib/utils'

type SortDirection = 'asc' | 'desc'
type SortKey = keyof CandidateRowView

const ALL_SECTORS = '__all__'

function numericFilter(value: string) {
  if (value.trim() === '') return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

function compareRows(left: CandidateRowView, right: CandidateRowView, key: SortKey, direction: SortDirection) {
  const a = left[key]
  const b = right[key]
  if (a === null) return 1
  if (b === null) return -1
  let result: number
  if (typeof a === 'number' && typeof b === 'number') result = a - b
  else if (typeof a === 'boolean' && typeof b === 'boolean') result = Number(a) - Number(b)
  else result = String(a).localeCompare(String(b), 'ja')
  return direction === 'asc' ? result : -result
}

function SortHeader({
  label,
  column,
  sortKey,
  direction,
  onSort,
  right = false,
}: {
  label: string
  column: SortKey
  sortKey: SortKey
  direction: SortDirection
  onSort: (key: SortKey) => void
  right?: boolean
}) {
  const active = column === sortKey
  const Icon = active ? direction === 'asc' ? ArrowUp : ArrowDown : ArrowUpDown
  return (
    <TableHead aria-sort={active ? direction === 'asc' ? 'ascending' : 'descending' : undefined} className={cn(right && 'text-right')}>
      <Button
        className={cn('-mx-2 text-muted-foreground', right && 'ml-auto -mr-2', active && 'text-foreground')}
        onClick={() => onSort(column)}
        size="xs"
        type="button"
        variant="ghost"
      >
        {label}<Icon aria-hidden="true" />
      </Button>
    </TableHead>
  )
}

function Metric({ value, digits = 2 }: { value: number | null; digits?: number }) {
  return value === null
    ? <span className="text-muted-foreground">{EMPTY}</span>
    : <span className="font-mono tabular-nums">{formatNumber(value, digits)}</span>
}

const PORTFOLIO_STATE_LABEL: Record<PortfolioState, string | null> = {
  unheld: null,
  held: '保有',
  reserved: '予約',
  held_and_reserved: '保有+予約',
}

function PortfolioStateBadge({ state }: { state: PortfolioState }) {
  const label = PORTFOLIO_STATE_LABEL[state]
  if (label === null) return <span className="text-muted-foreground">—</span>
  return <Badge variant={state === 'reserved' ? 'outline' : 'secondary'}>{label}</Badge>
}

function DataQualityCell({ flags }: { flags: string[] }) {
  if (flags.length === 0) return <span className="text-muted-foreground">—</span>
  return (
    <div className="flex flex-wrap gap-1">
      {flags.map((flag) => <Badge className="text-[10px]" key={flag} variant="outline">{flag}</Badge>)}
    </div>
  )
}

function ErSplitCell({ reversion, carry }: { reversion: number | null; carry: number | null }) {
  if (reversion === null && carry === null) return <span className="text-muted-foreground">—</span>
  return (
    <span className="font-mono text-xs tabular-nums">
      <PctBadge fraction value={reversion} /><span className="mx-0.5 text-muted-foreground">/</span><PctBadge fraction value={carry} />
    </span>
  )
}

function FilterField({ label, children, className }: { label: string; children: React.ReactNode; className?: string }) {
  return (
    <label className={cn('grid gap-1.5', className)}>
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
      {children}
    </label>
  )
}

function LayerHeading({ layer, title, note }: { layer: string; title: string; note: string }) {
  return (
    <div>
      <p className="text-sm font-medium text-muted-foreground">{layer}</p>
      <h2 className="text-xl font-semibold tracking-tight">{title}</h2>
      <p className="mt-1 text-sm text-muted-foreground">{note}</p>
    </div>
  )
}

export function StocksPage() {
  const [data, setData] = useState<ScreeningView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [sector, setSector] = useState('')
  const [heldOnly, setHeldOnly] = useState(false)
  const [researchOnly, setResearchOnly] = useState(false)
  const [perMax, setPerMax] = useState('')
  const [pbrMax, setPbrMax] = useState('')
  const [dividendMin, setDividendMin] = useState('')
  const [sortKey, setSortKey] = useState<SortKey>('er_annual')
  const [direction, setDirection] = useState<SortDirection>('desc')
  const [showAll, setShowAll] = useState(false)

  useEffect(() => {
    fetchJson<ScreeningView>('/api/screening/latest').then(setData).catch((reason: unknown) => {
      setError(reason instanceof Error ? reason.message : 'Stocks を読み込めませんでした')
    })
  }, [])

  const sectors = useMemo(() => Array.from(new Set(data?.rows.map((row) => row.sector_33).filter((value): value is string => value !== null))).sort((a, b) => a.localeCompare(b, 'ja')), [data])

  const rows = useMemo(() => {
    if (!data) return []
    const normalized = query.trim().toLocaleLowerCase('ja')
    const maxPer = numericFilter(perMax)
    const maxPbr = numericFilter(pbrMax)
    const minDividend = numericFilter(dividendMin)
    return data.rows.filter((row) => {
      if (normalized && !row.ticker.toLocaleLowerCase('ja').startsWith(normalized) && !(row.name ?? '').toLocaleLowerCase('ja').includes(normalized)) return false
      if (sector && row.sector_33 !== sector) return false
      if (heldOnly && row.portfolio_state !== 'held' && row.portfolio_state !== 'held_and_reserved') return false
      if (researchOnly && !row.has_research) return false
      if (maxPer !== null && (row.per_trailing === null || row.per_trailing > maxPer)) return false
      if (maxPbr !== null && (row.pbr === null || row.pbr > maxPbr)) return false
      if (minDividend !== null && (row.dividend_yield === null || row.dividend_yield < minDividend / 100)) return false
      return true
    }).sort((left, right) => compareRows(left, right, sortKey, direction))
  }, [data, query, sector, heldOnly, researchOnly, perMax, pbrMax, dividendMin, sortKey, direction])

  const onSort = (key: SortKey) => {
    if (key === sortKey) setDirection((current) => current === 'asc' ? 'desc' : 'asc')
    else {
      setSortKey(key)
      setDirection('desc')
    }
  }

  if (error) return <PageState message={error} title="Stocks read error" />
  if (!data) return <PageState message="候補を読み込んでいます…" title="Stocks" />
  if (!data.run) return <PageState message="screening run publication がありません" title="Stocks" />

  const visibleRows = showAll ? rows : rows.slice(0, 500)

  return (
    <>
      <AppShell />
      <main className="mx-auto grid max-w-[1600px] gap-5 px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
        <header className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">Stocks</h1>
            <p className="mt-1 text-sm text-muted-foreground">個別株の機械スクリーニング（Fact）と深掘り候補（Judgment）</p>
          </div>
          <div className="flex flex-col items-start gap-2 lg:items-end">
            {data.run.stale && <StaleBadge detail={`${LABEL.asOf} ${data.run.asof_date}（7 日超）`} />}
            <dl className="grid grid-cols-2 gap-x-8 gap-y-2 sm:grid-cols-4">
              {[
                [LABEL.asOf, data.run.asof_date],
                [LABEL.run, formatJstDateTime(data.run.run_at)],
                ['UNIVERSE', data.run.universe_size.toLocaleString('ja-JP')],
                ['CANDIDATES', data.run.candidate_count.toLocaleString('ja-JP')],
              ].map(([label, value]) => (
                <div key={label}>
                  <dt className="text-[10px] font-semibold tracking-wider text-muted-foreground">{label}</dt>
                  <dd className="mt-1 font-mono text-sm font-medium tabular-nums">{value}</dd>
                </div>
              ))}
            </dl>
          </div>
        </header>

        <LayerHeading layer="Fact ・ 機械" note="機械 selection と全通過 candidates。review 済み判断ではありません" title="スクリーニング結果" />

        <Card className="gap-3 py-5 shadow-sm"><CardHeader className="px-5"><CardTitle className="text-base">Machine recommendations</CardTitle><CardDescription>機械 selection。review 済み判断ではありません</CardDescription></CardHeader><CardContent className="grid gap-3 px-5">{data.selections.length === 0 ? <p className="text-sm text-muted-foreground">selection 未作成</p> : data.selections.map((selection) => <div className="rounded-lg border p-3" key={selection.selection_id}><div className="mb-2 flex flex-wrap gap-2"><Badge>{selection.profile}</Badge><span className="font-mono text-xs text-muted-foreground">{selection.selection_id}</span></div><div className="flex flex-wrap gap-2">{selection.recommendations.map((item, index) => <Badge key={String(item.ticker ?? index)} variant="secondary">{String(item.ticker ?? 'unknown')}</Badge>)}</div>{selection.longlist.length > 0 && <p className="mt-2 text-xs text-muted-foreground">Longlist: {selection.longlist.length} 件（recommendation とは別）</p>}</div>)}</CardContent></Card>

        <Card className="gap-4 py-5 shadow-sm">
          <CardHeader className="px-5 sm:px-6">
            <CardTitle className="text-base">フィルター</CardTitle>
            <CardDescription>銘柄・sector・主要指標で候補を絞り込みます</CardDescription>
          </CardHeader>
          <CardContent className="grid items-end gap-3 px-5 sm:grid-cols-2 sm:px-6 lg:grid-cols-[1.5fr_1.1fr_repeat(3,minmax(100px,.55fr))]">
            <FilterField label="銘柄">
              <div className="relative">
                <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
                <Input className="pl-8" onChange={(event) => setQuery(event.target.value)} placeholder="ticker 前方一致 / 銘柄名" type="search" value={query} />
              </div>
            </FilterField>
            <FilterField label="sector">
              <Select onValueChange={(value) => setSector(value === ALL_SECTORS ? '' : value)} value={sector || ALL_SECTORS}>
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value={ALL_SECTORS}>すべて</SelectItem>
                  {sectors.map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}
                </SelectContent>
              </Select>
            </FilterField>
            <FilterField label="PER ≤"><Input inputMode="decimal" onChange={(event) => setPerMax(event.target.value)} placeholder="無条件" value={perMax} /></FilterField>
            <FilterField label="PBR ≤"><Input inputMode="decimal" onChange={(event) => setPbrMax(event.target.value)} placeholder="無条件" value={pbrMax} /></FilterField>
            <FilterField label="配当 ≥ %"><Input inputMode="decimal" onChange={(event) => setDividendMin(event.target.value)} placeholder="無条件" value={dividendMin} /></FilterField>
          </CardContent>
          <CardContent className="flex flex-wrap gap-5 border-t px-5 pt-4 sm:px-6">
            <label className="flex items-center gap-2 text-sm font-medium">
              <Checkbox checked={heldOnly} onCheckedChange={(checked) => setHeldOnly(checked === true)} />
              保有のみ
            </label>
            <label className="flex items-center gap-2 text-sm font-medium">
              <Checkbox checked={researchOnly} onCheckedChange={(checked) => setResearchOnly(checked === true)} />
              research 有り
            </label>
          </CardContent>
        </Card>

        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
          <strong className="text-sm text-foreground">{rows.length.toLocaleString('ja-JP')} 件</strong>
          <span>default: E[r] 降順（canonical ranking と同順・calibration replay で trap 最小）。割安score（= reversion + 0.5×min(carry, 15%) − 0.005×品質flag数）は dislocation lens 用の sort 可能列 / null は末尾</span>
          {!showAll && rows.length > 500 && <span>先頭 500 件を表示</span>}
          <code className="ml-auto hidden max-w-md truncate font-mono lg:block" title={data.run.source_path}>{data.run.source_path}</code>
        </div>

        <div><h3 className="text-lg font-semibold">全通過 candidates</h3><p className="text-sm text-muted-foreground">machine recommendation / shortlist とは異なる母集団です</p></div>
        <Card className="overflow-hidden py-0 shadow-sm">
          <Table className="min-w-[2280px] text-xs">
            <TableHeader className="bg-muted/70">
              <TableRow className="hover:bg-transparent">
                <SortHeader column="ticker" direction={direction} label="ticker" onSort={onSort} sortKey={sortKey} />
                <SortHeader column="name" direction={direction} label="name" onSort={onSort} sortKey={sortKey} />
                <SortHeader column="sector_33" direction={direction} label="sector" onSort={onSort} sortKey={sortKey} />
                <SortHeader column="market_cap_oku" direction={direction} label="時価総額(億)" onSort={onSort} right sortKey={sortKey} />
                <SortHeader column="avg_turnover_oku" direction={direction} label="売買代金(億)" onSort={onSort} right sortKey={sortKey} />
                <SortHeader column="per_trailing" direction={direction} label="PER" onSort={onSort} right sortKey={sortKey} />
                <SortHeader column="per_forward" direction={direction} label="PER(F)" onSort={onSort} right sortKey={sortKey} />
                <SortHeader column="pbr" direction={direction} label="PBR" onSort={onSort} right sortKey={sortKey} />
                <SortHeader column="dividend_yield" direction={direction} label="配当" onSort={onSort} right sortKey={sortKey} />
                <SortHeader column="bargain_score" direction={direction} label="割安score" onSort={onSort} right sortKey={sortKey} />
                <SortHeader column="er_annual" direction={direction} label="E[r]" onSort={onSort} right sortKey={sortKey} />
                <TableHead className="text-right">rev/carry</TableHead>
                <SortHeader column="net_cash_to_market_cap" direction={direction} label="Net cash" onSort={onSort} right sortKey={sortKey} />
                <SortHeader column="fcf_yield" direction={direction} label="FCF yield" onSort={onSort} right sortKey={sortKey} />
                <SortHeader column="sales_yoy" direction={direction} label="売上YoY" onSort={onSort} right sortKey={sortKey} />
                <SortHeader column="operating_profit_yoy" direction={direction} label="営業益YoY" onSort={onSort} right sortKey={sortKey} />
                <SortHeader column="price_change_20d" direction={direction} label="20d" onSort={onSort} right sortKey={sortKey} />
                <SortHeader column="gap_from_52w_low" direction={direction} label="52w low" onSort={onSort} right sortKey={sortKey} />
                <SortHeader column="sector_relative_strength_percentile" direction={direction} label="RS%" onSort={onSort} right sortKey={sortKey} />
                <SortHeader column="next_earnings_date" direction={direction} label="決算予定" onSort={onSort} sortKey={sortKey} />
                <TableHead>品質</TableHead>
                <SortHeader column="portfolio_state" direction={direction} label="保有/予約" onSort={onSort} sortKey={sortKey} />
                <SortHeader column="has_research" direction={direction} label="research" onSort={onSort} sortKey={sortKey} />
                <TableHead className="w-12"><span className="sr-only">チャート</span></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {visibleRows.map((row) => (
                <TableRow key={row.ticker}>
                  <TableCell><Link className="font-mono font-semibold underline-offset-4 hover:underline" to={`/securities/${row.ticker}`}>{row.ticker}</Link></TableCell>
                  <TableCell className="max-w-52 truncate font-medium" title={row.name ?? undefined}>{row.name ?? '—'}</TableCell>
                  <TableCell className="max-w-40 truncate text-muted-foreground" title={row.sector_33 ?? undefined}>{row.sector_33 ?? '—'}</TableCell>
                  <TableCell className="text-right"><Metric digits={0} value={row.market_cap_oku} /></TableCell>
                  <TableCell className="text-right"><Metric digits={1} value={row.avg_turnover_oku} /></TableCell>
                  <TableCell className="text-right"><Metric value={row.per_trailing} /></TableCell>
                  <TableCell className="text-right"><Metric value={row.per_forward} /></TableCell>
                  <TableCell className="text-right"><Metric value={row.pbr} /></TableCell>
                  <TableCell className="text-right"><PctBadge fraction value={row.dividend_yield} /></TableCell>
                  <TableCell className="text-right"><PctBadge fraction value={row.bargain_score} /></TableCell>
                  <TableCell className="text-right"><PctBadge fraction value={row.er_annual} /></TableCell>
                  <TableCell className="text-right"><ErSplitCell carry={row.er_carry_annual} reversion={row.er_reversion_annual} /></TableCell>
                  <TableCell className="text-right"><PctBadge fraction value={row.net_cash_to_market_cap} /></TableCell>
                  <TableCell className="text-right"><PctBadge fraction value={row.fcf_yield} /></TableCell>
                  <TableCell className="text-right"><PctBadge fraction value={row.sales_yoy} /></TableCell>
                  <TableCell className="text-right"><PctBadge fraction value={row.operating_profit_yoy} /></TableCell>
                  <TableCell className="text-right"><PctBadge fraction value={row.price_change_20d} /></TableCell>
                  <TableCell className="text-right"><PctBadge fraction value={row.gap_from_52w_low} /></TableCell>
                  <TableCell className="text-right"><PctBadge fraction value={row.sector_relative_strength_percentile} /></TableCell>
                  <TableCell className="font-mono tabular-nums">{row.next_earnings_date ?? LABEL.earningsTbd}</TableCell>
                  <TableCell><DataQualityCell flags={row.data_quality_flags} /></TableCell>
                  <TableCell><PortfolioStateBadge state={row.portfolio_state} /></TableCell>
                  <TableCell>{row.has_research ? <Badge variant="outline">有</Badge> : <span className="text-muted-foreground">—</span>}</TableCell>
                  <TableCell><TradingViewButton ticker={row.ticker} /></TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>

        {!showAll && rows.length > 500 && (
          <Button className="mx-auto" onClick={() => setShowAll(true)} type="button" variant="outline">全 {rows.length.toLocaleString('ja-JP')} 件を表示</Button>
        )}

        <LayerHeading layer="Judgment ・ AI + 人間" note="OP3 gate で longlist から選んだ深掘り候補。最終 buy 提案ではありません" title="Shortlist" />

        <Card className="gap-3 py-5 shadow-sm"><CardHeader className="px-5"><CardTitle className="text-base">Shortlist</CardTitle><CardDescription>AI / 人間 review 後に明示 publish された判断 gate</CardDescription></CardHeader><CardContent className="grid gap-3 px-5">{data.shortlists.length === 0 ? <p className="text-sm font-medium text-warning">shortlist 未作成</p> : data.shortlists.map((shortlist) => { const selectedCount = shortlist.entries.filter((entry) => entry.decision === 'selected').length; return <div className="rounded-lg border p-3" key={shortlist.shortlist_id}><p className="mb-2 font-mono text-xs text-muted-foreground">{shortlist.shortlist_id}</p><div className="mb-3 flex flex-wrap gap-1.5">{shortlist.entries.filter((entry) => entry.decision === 'selected').map((entry) => <Link key={entry.ticker} to={`/securities/${entry.ticker}`}><Badge>{entry.ticker}</Badge></Link>)}</div><p className="text-xs text-muted-foreground">selected {selectedCount} 件・rejected {shortlist.entries.length - selectedCount} 件</p></div> })}<Button asChild className="w-full" size="sm" variant="outline"><Link to="/stocks/shortlist">レビュー面を開く（narrative + 機械値）→</Link></Button></CardContent></Card>
      </main>
    </>
  )
}
