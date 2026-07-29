import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowDown, ArrowUp, ArrowUpDown, Search } from 'lucide-react'

import { fetchJson } from '../api/client'
import type {
  BargainAssessmentSummaryView,
  CandidateRowView,
  OperationsView,
  ProposalView,
  ScreeningHistoryRunView,
  ScreeningHistoryView,
  ScreeningRunView,
  ScreeningView,
} from '../api/types'
import { AsOfBadge } from '../components/AsOfBadge'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageShell } from '../components/PageShell'
import { SectionCard } from '../components/SectionCard'
import { PageState } from '../components/PageState'
import { PctBadge } from '../components/PctBadge'
import { PortfolioStateBadge } from '../components/PortfolioStateBadge'
import { StaleBadge } from '../components/StaleBadge'
import { TradingViewButton } from '../components/TradingViewButton'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Card, CardContent } from '../components/ui/card'
import { Checkbox } from '../components/ui/checkbox'
import { Input } from '../components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../components/ui/table'
import { ASSESSMENT_RESULT, ASSESSMENT_TONE_CLASS } from '../lib/assessment'
import { EMPTY, formatJstDate, formatJstDateTime, formatNumber, formatYen } from '../lib/format'
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

function candidateDateLabel(value: string, latest: string) {
  if (value === latest) return `${value}（最新）`
  const today = new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Tokyo',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(new Date())
  const days = Math.round(
    (Date.parse(`${today}T00:00:00Z`) - Date.parse(`${value}T00:00:00Z`)) / 86_400_000,
  )
  if (days === 0) return `${value}（今日）`
  if (days === 1) return `${value}（昨日）`
  return value
}

// The cycle's answer, newest first. The head assessment is the current one; the rest are
// the record of what earlier cycles concluded and why.
function AssessmentIndex({ assessments }: { assessments: readonly BargainAssessmentSummaryView[] }) {
  if (assessments.length === 0) {
    return (
      <Card className="py-5 shadow-sm">
        <CardContent className="px-5 text-sm text-muted-foreground">
          割安機会評価はまだ publish されていません。shortlist から個別リサーチを経て作成します。
        </CardContent>
      </Card>
    )
  }
  return (
    <div className="grid gap-3">
      {assessments.map((assessment, index) => {
        const result = ASSESSMENT_RESULT[assessment.result] ?? { label: assessment.result, tone: 'muted' as const }
        return (
          <Card className={cn('gap-2 py-4 shadow-sm', index === 0 && 'border-foreground/25')} key={assessment.assessment_id}>
            <CardContent className="flex flex-wrap items-center justify-between gap-x-5 gap-y-2 px-5">
              <div className="grid gap-1">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge className={cn('font-semibold', ASSESSMENT_TONE_CLASS[result.tone])}>{result.label}</Badge>
                  {index === 0 && <Badge variant="outline">最新</Badge>}
                  {assessment.selected_ticker !== null && <span className="font-mono text-sm font-semibold">{assessment.selected_ticker}</span>}
                </div>
                <p className="text-sm font-medium">{assessment.headline}</p>
                <p className="text-xs text-muted-foreground">
                  {LABEL.asOf} {assessment.as_of} · {LABEL.published} {formatJstDateTime(assessment.published_at)} · 深掘り {assessment.lane_count} 銘柄
                </p>
              </div>
              <Button asChild size="sm" variant="outline">
                <Link to={`/stocks/assessments/${assessment.assessment_id}`}>提案レポートを読む →</Link>
              </Button>
            </CardContent>
          </Card>
        )
      })}
    </div>
  )
}

const PROPOSAL_STATUS_LABEL: Record<string, string> = {
  pending: '判断待ち',
  approved: '承認',
  deferred: '保留',
  rejected: '見送り',
}

function planField(payload: Record<string, unknown>, key: string): unknown {
  const plan = payload.planned_limit
  if (typeof plan !== 'object' || plan === null) return undefined
  return (plan as Record<string, unknown>)[key]
}

// The bargain assessment shows the proposal it published with, but a proposal exists
// from the moment research concludes — before any assessment, and in cycles that end
// with no purchase at all. This is where that state is readable on its own.
function ProposalList({ proposals }: { proposals: readonly ProposalView[] }) {
  if (proposals.length === 0) {
    return (
      <Card className="py-5 shadow-sm">
        <CardContent className="px-5 text-sm text-muted-foreground">
          売買提案はありません。research の結論が proposal になると、指値・数量・期限とその判断状況がここに出ます。
        </CardContent>
      </Card>
    )
  }
  return (
    <Card className="divide-y py-0 shadow-sm">
      {proposals.map((item) => {
        const limit = planField(item.payload, 'limit_price_yen')
        const quantity = planField(item.payload, 'quantity')
        const expiresAt = planField(item.payload, 'expires_at')
        return (
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 px-5 py-3 sm:px-6" key={item.proposal_id}>
            <Link className="min-w-[4rem] shrink-0 font-mono font-semibold underline-offset-4 hover:underline" to={`/securities/${item.ticker}`}>{item.ticker}</Link>
            <Badge className="shrink-0" variant="outline">{PROPOSAL_STATUS_LABEL[item.status] ?? item.status}</Badge>
            <div className="flex min-w-[16rem] flex-1 flex-wrap gap-x-4 gap-y-1 text-xs">
              <span>指値 <span className="font-mono tabular-nums text-foreground">{typeof limit === 'string' || typeof limit === 'number' ? formatYen(Number(limit)) : EMPTY}</span></span>
              <span>数量 <span className="font-mono tabular-nums text-foreground">{typeof quantity === 'number' ? `${quantity.toLocaleString('ja-JP')} 株` : EMPTY}</span></span>
              <span>期限 <span className="font-mono tabular-nums text-foreground">{typeof expiresAt === 'string' ? formatJstDate(expiresAt.slice(0, 10)) : EMPTY}</span></span>
            </div>
            <div className="flex shrink-0 flex-wrap gap-x-3 text-[11px] text-muted-foreground">
              <span>作成 {formatJstDate(item.created_at.slice(0, 10))}</span>
              {item.decided_at !== null && <span>決定 {formatJstDate(item.decided_at.slice(0, 10))}</span>}
            </div>
          </div>
        )
      })}
    </Card>
  )
}

export function StocksPage() {
  const [data, setData] = useState<ScreeningView | null>(null)
  const [historyDates, setHistoryDates] = useState<string[]>([])
  const [selectedDate, setSelectedDate] = useState('')
  const [historicalRun, setHistoricalRun] = useState<ScreeningHistoryRunView | null>(null)
  const [historyLoading, setHistoryLoading] = useState(false)
  const [historyError, setHistoryError] = useState<string | null>(null)
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
  // Proposals live in the operations view, which the page reads on its own rather than
  // widening the screening projection with state that does not come from a run.
  const [proposals, setProposals] = useState<readonly ProposalView[]>([])

  useEffect(() => {
    Promise.all([
      fetchJson<ScreeningView>('/api/screening/latest'),
      fetchJson<ScreeningHistoryView>('/api/screening/history').catch(() => ({ dates: [] })),
    ]).then(([screening, history]) => {
      setData(screening)
      const latest = screening.run?.asof_date ?? ''
      setSelectedDate(latest)
      setHistoryDates(Array.from(new Set([latest, ...history.dates])).filter(Boolean).sort().reverse())
    }).catch((reason: unknown) => {
      setError(reason instanceof Error ? reason.message : 'Stocks を読み込めませんでした')
    })
    // A missing operations view leaves the section empty rather than failing the page.
    fetchJson<OperationsView>('/api/operations')
      .then((operations) => setProposals(operations.proposals))
      .catch(() => setProposals([]))
  }, [])

  const activeRun: ScreeningRunView | null = historicalRun === null
    ? data?.run ?? null
    : historicalRun.run
  const candidateRows = useMemo(
    () => historicalRun === null || data === null
      ? data?.rows ?? []
      : historicalRun.rows,
    [data, historicalRun],
  )
  const sectors = useMemo(() => Array.from(new Set(candidateRows.map((row) => row.sector_33).filter((value): value is string => value !== null))).sort((a, b) => a.localeCompare(b, 'ja')), [candidateRows])

  const rows = useMemo(() => {
    if (!data) return []
    const normalized = query.trim().toLocaleLowerCase('ja')
    const maxPer = numericFilter(perMax)
    const maxPbr = numericFilter(pbrMax)
    const minDividend = numericFilter(dividendMin)
    return candidateRows.filter((row) => {
      if (normalized && !row.ticker.toLocaleLowerCase('ja').startsWith(normalized) && !(row.name ?? '').toLocaleLowerCase('ja').includes(normalized)) return false
      if (sector && row.sector_33 !== sector) return false
      if (heldOnly && row.portfolio_state !== 'held' && row.portfolio_state !== 'held_and_reserved') return false
      if (researchOnly && !row.has_research) return false
      if (maxPer !== null && (row.per_trailing === null || row.per_trailing > maxPer)) return false
      if (maxPbr !== null && (row.pbr === null || row.pbr > maxPbr)) return false
      if (minDividend !== null && (row.dividend_yield === null || row.dividend_yield < minDividend / 100)) return false
      return true
    }).sort((left, right) => compareRows(left, right, sortKey, direction))
  }, [data, candidateRows, query, sector, heldOnly, researchOnly, perMax, pbrMax, dividendMin, sortKey, direction])

  const selectCandidateDate = (value: string) => {
    if (!data?.run) return
    setHistoryError(null)
    if (value === data.run.asof_date) {
      setSelectedDate(value)
      setHistoricalRun(null)
      setSector('')
      setShowAll(false)
      return
    }
    setHistoryLoading(true)
    fetchJson<ScreeningHistoryRunView>(`/api/screening/history/${value}`)
      .then((history) => {
        setHistoricalRun(history)
        setSelectedDate(value)
        setSector('')
        setShowAll(false)
      })
      .catch((reason: unknown) => {
        setHistoryDates((dates) => dates.filter((date) => date !== value))
        setHistoryError(reason instanceof Error ? reason.message : 'Candidates 履歴を読み込めませんでした')
      })
      .finally(() => setHistoryLoading(false))
  }

  const onSort = (key: SortKey) => {
    if (key === sortKey) setDirection((current) => current === 'asc' ? 'desc' : 'asc')
    else {
      setSortKey(key)
      setDirection('desc')
    }
  }

  if (error) return <PageState message={error} title="Stocks read error" />
  if (!data) return <LoadingPage label="候補を読み込んでいます" />
  if (!data.run || !activeRun) return <PageState message="screening run publication がありません" title="Stocks" />

  const visibleRows = showAll ? rows : rows.slice(0, 500)

  return (
    <PageShell
      meta={(
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-xs text-muted-foreground tabular-nums">
          {data.run.stale && <StaleBadge detail="7 日超" />}
          <AsOfBadge value={data.run.asof_date} />
          <span>対象銘柄 {data.run.universe_size.toLocaleString('ja-JP')}</span>
          <span>Candidates {data.run.candidate_count.toLocaleString('ja-JP')}</span>
        </div>
      )}
      title="Stocks"
    >
      <section className="grid gap-3">
        <h2 className="text-xl font-semibold tracking-tight">割安機会評価</h2>
        <AssessmentIndex assessments={data.assessments} />
      </section>

      <section className="grid gap-3">
        <div className="flex items-center gap-2">
          <h2 className="text-xl font-semibold tracking-tight">売買提案</h2>
          <Badge variant="secondary">{proposals.length} 件</Badge>
        </div>
        <ProposalList proposals={proposals} />
      </section>

      <section className="grid gap-3">
        <h2 className="text-xl font-semibold tracking-tight">リサーチ候補選定</h2>
        <div className="grid gap-4 lg:grid-cols-2">
          <SectionCard description="OP3 gate で選んだ深掘り候補" padded title="Shortlist">
            <div className="grid gap-3">{data.shortlists.length === 0 ? <p className="text-sm font-medium text-warning">Shortlist 未作成</p> : data.shortlists.map((shortlist) => { const selectedCount = shortlist.entries.filter((entry) => entry.decision === 'selected').length; return <div className="rounded-lg border p-3" key={shortlist.shortlist_id}><p className="mb-2 font-mono text-xs text-muted-foreground">{shortlist.shortlist_id}</p><div className="mb-3 flex flex-wrap gap-1.5">{shortlist.entries.filter((entry) => entry.decision === 'selected').map((entry) => <Link key={entry.ticker} to={`/securities/${entry.ticker}`}><Badge>{entry.ticker}</Badge></Link>)}</div><p className="text-xs text-muted-foreground">選定 {selectedCount} 件・見送り {shortlist.entries.length - selectedCount} 件</p></div> })}<Button asChild className="w-full" size="sm" variant="outline"><Link to="/stocks/shortlist">Shortlist の詳細を見る →</Link></Button></div>
          </SectionCard>
          <SectionCard description="E[r] ranking による OP3 レビューの入力母集団" padded title="Longlist">
            <div className="grid gap-3">{data.selections.length === 0 ? <p className="text-sm text-muted-foreground">Longlist はありません</p> : data.selections.map((selection) => <div className="rounded-lg border p-3" key={selection.selection_id}><div className="mb-2 flex flex-wrap gap-2"><Badge>{selection.profile}</Badge><span className="font-mono text-xs text-muted-foreground">{selection.selection_id}</span></div><div className="flex flex-wrap gap-2">{selection.longlist.map((item, index) => <Badge key={String(item.ticker ?? index)} variant="secondary">{String(item.ticker ?? 'unknown')}</Badge>)}</div><p className="mt-2 text-xs text-muted-foreground">{selection.longlist.length} 件</p></div>)}</div>
          </SectionCard>
        </div>
      </section>

      <section className="grid gap-3">
        <h2 className="text-xl font-semibold tracking-tight">Candidates</h2>
        <Card className="gap-4 py-5 shadow-sm">
          <CardContent className="grid items-end gap-3 px-5 sm:grid-cols-2 sm:px-6 lg:grid-cols-[1fr_1.4fr_1.1fr_repeat(3,minmax(96px,.55fr))]">
            <FilterField label="日付">
              <Select disabled={historyLoading} onValueChange={selectCandidateDate} value={selectedDate}>
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {historyDates.map((item) => <SelectItem key={item} value={item}>{candidateDateLabel(item, data.run?.asof_date ?? '')}</SelectItem>)}
                </SelectContent>
              </Select>
            </FilterField>
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
            {historyError && <p className="basis-full text-sm text-destructive" role="alert">{historyError}</p>}
          </CardContent>
        </Card>

        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
          <strong className="text-sm text-foreground">{rows.length.toLocaleString('ja-JP')} 件</strong>
          <span>{LABEL.asOf} {activeRun.asof_date}</span>
          {activeRun.stale && <StaleBadge detail={`${LABEL.asOf} ${activeRun.asof_date}（7 日超）`} />}
          <span>E[r] 降順</span>
          {!showAll && rows.length > 500 && <span>先頭 500 件を表示</span>}
        </div>

        <Card className="overflow-hidden py-0 shadow-sm">
          <Table className="min-w-[2280px] text-xs">
            <TableHeader className="bg-muted/70">
              <TableRow className="hover:bg-transparent">
                <SortHeader column="ticker" direction={direction} label="ticker" onSort={onSort} sortKey={sortKey} />
                <SortHeader column="name" direction={direction} label="name" onSort={onSort} sortKey={sortKey} />
                <SortHeader column="sector_33" direction={direction} label="sector" onSort={onSort} sortKey={sortKey} />
                <SortHeader column="er_annual" direction={direction} label="E[r]" onSort={onSort} right sortKey={sortKey} />
                <TableHead className="text-right">rev/carry</TableHead>
                <SortHeader column="bargain_score" direction={direction} label="割安score" onSort={onSort} right sortKey={sortKey} />
                <SortHeader column="fair_value_gap_pct" direction={direction} label="FV乖離" onSort={onSort} right sortKey={sortKey} />
                <SortHeader column="per_forward" direction={direction} label="PER(F)" onSort={onSort} right sortKey={sortKey} />
                <SortHeader column="per_trailing" direction={direction} label="PER" onSort={onSort} right sortKey={sortKey} />
                <SortHeader column="pbr" direction={direction} label="PBR" onSort={onSort} right sortKey={sortKey} />
                <SortHeader column="dividend_yield" direction={direction} label="配当" onSort={onSort} right sortKey={sortKey} />
                <SortHeader column="net_cash_to_market_cap" direction={direction} label="Net cash" onSort={onSort} right sortKey={sortKey} />
                <SortHeader column="fcf_yield" direction={direction} label="FCF yield" onSort={onSort} right sortKey={sortKey} />
                <SortHeader column="sales_yoy" direction={direction} label="売上YoY" onSort={onSort} right sortKey={sortKey} />
                <SortHeader column="operating_profit_yoy" direction={direction} label="営業益YoY" onSort={onSort} right sortKey={sortKey} />
                <SortHeader column="market_cap_oku" direction={direction} label="時価総額(億)" onSort={onSort} right sortKey={sortKey} />
                <SortHeader column="avg_turnover_oku" direction={direction} label="売買代金(億)" onSort={onSort} right sortKey={sortKey} />
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
                  <TableCell className="text-right"><PctBadge fraction value={row.er_annual} /></TableCell>
                  <TableCell className="text-right"><ErSplitCell carry={row.er_carry_annual} reversion={row.er_reversion_annual} /></TableCell>
                  <TableCell className="text-right"><PctBadge fraction value={row.bargain_score} /></TableCell>
                  <TableCell className="text-right" title={row.fair_value_anchor_yen === null ? 'longlist 外のため FV アンカーなし' : `FV アンカー ${formatNumber(row.fair_value_anchor_yen, 0)} 円`}><PctBadge value={row.fair_value_gap_pct} /></TableCell>
                  <TableCell className="text-right"><Metric value={row.per_forward} /></TableCell>
                  <TableCell className="text-right"><Metric value={row.per_trailing} /></TableCell>
                  <TableCell className="text-right"><Metric value={row.pbr} /></TableCell>
                  <TableCell className="text-right"><PctBadge fraction value={row.dividend_yield} /></TableCell>
                  <TableCell className="text-right"><PctBadge fraction value={row.net_cash_to_market_cap} /></TableCell>
                  <TableCell className="text-right"><PctBadge fraction value={row.fcf_yield} /></TableCell>
                  <TableCell className="text-right"><PctBadge fraction value={row.sales_yoy} /></TableCell>
                  <TableCell className="text-right"><PctBadge fraction value={row.operating_profit_yoy} /></TableCell>
                  <TableCell className="text-right"><Metric digits={0} value={row.market_cap_oku} /></TableCell>
                  <TableCell className="text-right"><Metric digits={1} value={row.avg_turnover_oku} /></TableCell>
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
      </section>
    </PageShell>
  )
}
