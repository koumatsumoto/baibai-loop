import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { ArrowDown, ArrowUp, Search } from 'lucide-react'
import { Link } from 'react-router'

import type { SecurityAnalysisRowView } from '../../api/types'
import { SectionCard } from '../../components/SectionCard'
import { Badge } from '../../components/ui/badge'
import { Button } from '../../components/ui/button'
import { Input } from '../../components/ui/input'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../../components/ui/table'
import { EMPTY, formatNumber } from '../../lib/format'
import { parseOptionalNumber, projectSecurityRows, SECURITY_PAGE_SIZE, type SecurityFilters, type SecuritySort, type SecuritySortKey } from '../../lib/stocks'

function number(value: number | null, digits = 2): string {
  return value === null ? EMPTY : formatNumber(value, digits)
}

function ratio(value: number | null): string {
  return value === null ? EMPTY : `${formatNumber(value * 100, 1)}%`
}

function percent(value: number | null): string {
  return value === null ? EMPTY : `${formatNumber(value, 1)}%`
}

function SortHead({ label, column, sort, onSort, className }: { label: string; column: SecuritySortKey; sort: SecuritySort; onSort: (column: SecuritySortKey) => void; className?: string }) {
  const active = sort.key === column
  const Icon = sort.direction === 'asc' ? ArrowUp : ArrowDown
  const nextDirection = active && sort.direction === 'asc' ? '降順' : '昇順'
  return <TableHead aria-sort={active ? (sort.direction === 'asc' ? 'ascending' : 'descending') : 'none'} className={className}><button aria-label={`${label}を${nextDirection}で並べ替え`} className="inline-flex items-center gap-1 hover:underline focus-visible:outline-none focus-visible:ring-2" onClick={() => onSort(column)} type="button">{label}{active && <Icon aria-hidden="true" className="size-3" />}</button></TableHead>
}

function FilterLabel({ title, children }: { title: string; children: ReactNode }) {
  return <label className="grid gap-1"><span className="text-xs font-medium text-muted-foreground">{title}</span>{children}</label>
}

export function SecurityAnalysisSection({ rows, runAsOf }: { rows: SecurityAnalysisRowView[]; runAsOf: string }) {
  const sectors = useMemo(() => [...new Set(rows.map((row) => row.sector_33).filter((item): item is string => item !== null))].sort((a, b) => a.localeCompare(b, 'ja-JP')), [rows])
  const [query, setQuery] = useState('')
  const [sector, setSector] = useState('all')
  const [perMax, setPerMax] = useState('')
  const [pbrMax, setPbrMax] = useState('')
  const [dividendMin, setDividendMin] = useState('')
  const [heldOnly, setHeldOnly] = useState(false)
  const [researchOnly, setResearchOnly] = useState(false)
  const [detail, setDetail] = useState(false)
  const [sort, setSort] = useState<SecuritySort>({ key: 'ticker', direction: 'asc' })
  const [page, setPage] = useState(1)

  const filters: SecurityFilters = useMemo(() => ({
    query,
    sector,
    perMax: parseOptionalNumber(perMax),
    pbrMax: parseOptionalNumber(pbrMax),
    dividendMinPct: parseOptionalNumber(dividendMin),
    heldOnly,
    researchOnly,
  }), [query, sector, perMax, pbrMax, dividendMin, heldOnly, researchOnly])
  const projection = useMemo(() => projectSecurityRows(rows, filters, sort, page), [rows, filters, sort, page])

  useEffect(() => setPage(1), [query, sector, perMax, pbrMax, dividendMin, heldOnly, researchOnly])

  const onSort = (key: SecuritySortKey) => {
    setSort((current) => current.key === key ? { key, direction: current.direction === 'asc' ? 'desc' : 'asc' } : { key, direction: 'asc' })
  }
  const start = projection.filteredTotal === 0 ? 0 : (projection.page - 1) * SECURITY_PAGE_SIZE + 1
  const end = Math.min(projection.page * SECURITY_PAGE_SIZE, projection.filteredTotal)

  return (
    <SectionCard description={`全分析母集団を探索。基準 ${runAsOf}。Review Set の選定順とは独立。`} title="Security Analysis">
      <div className="grid gap-4 border-b px-5 py-4 sm:px-6">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-6">
          <FilterLabel title="検索">
            <div className="relative lg:col-span-2"><Search aria-hidden="true" className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" /><Input aria-label="tickerまたは会社名を検索" className="pl-8" onChange={(event) => setQuery(event.target.value)} placeholder="ticker前方 / 名称部分" value={query} /></div>
          </FilterLabel>
          <FilterLabel title="sector"><select className="h-9 rounded-md border bg-background px-3 text-sm" onChange={(event) => setSector(event.target.value)} value={sector}><option value="all">すべて</option>{sectors.map((item) => <option key={item} value={item}>{item}</option>)}</select></FilterLabel>
          <FilterLabel title="PER ≤"><Input inputMode="decimal" onChange={(event) => setPerMax(event.target.value)} value={perMax} /></FilterLabel>
          <FilterLabel title="PBR ≤"><Input inputMode="decimal" onChange={(event) => setPbrMax(event.target.value)} value={pbrMax} /></FilterLabel>
          <FilterLabel title="配当 ≥ %"><Input inputMode="decimal" onChange={(event) => setDividendMin(event.target.value)} value={dividendMin} /></FilterLabel>
        </div>
        <div className="flex flex-wrap items-center gap-4 text-sm">
          <label className="inline-flex items-center gap-2"><input checked={heldOnly} onChange={(event) => setHeldOnly(event.target.checked)} type="checkbox" />保有のみ</label>
          <label className="inline-flex items-center gap-2"><input checked={researchOnly} onChange={(event) => setResearchOnly(event.target.checked)} type="checkbox" />researchありのみ</label>
          <label className="inline-flex items-center gap-2"><input checked={detail} onChange={(event) => setDetail(event.target.checked)} type="checkbox" />詳細列を表示</label>
          <span className="ml-auto text-xs text-muted-foreground">{start}–{end} / {projection.filteredTotal} 件</span>
        </div>
      </div>
      <Table className={detail ? 'min-w-[2600px]' : 'min-w-[1900px]'}>
        <TableHeader><TableRow>
          <SortHead column="ticker" label="ticker / name" onSort={onSort} sort={sort} /><SortHead column="sector_33" label="sector" onSort={onSort} sort={sort} />
          <SortHead className="text-right" column="market_cap_oku" label="market cap" onSort={onSort} sort={sort} />
          <SortHead className="text-right" column="per_forward" label="PER F" onSort={onSort} sort={sort} /><SortHead className="text-right" column="per_trailing" label="PER T" onSort={onSort} sort={sort} /><SortHead className="text-right" column="normalized_per_3fy" label="Norm PER 3FY" onSort={onSort} sort={sort} />
          <SortHead className="text-right" column="pbr" label="PBR" onSort={onSort} sort={sort} /><SortHead className="text-right" column="dividend_yield" label="dividend" onSort={onSort} sort={sort} />
          <SortHead className="text-right" column="net_cash_to_market_cap" label="Net cash / MC" onSort={onSort} sort={sort} /><SortHead className="text-right" column="fcf_yield" label="FCF yield" onSort={onSort} sort={sort} />
          <SortHead className="text-right" column="sales_yoy" label="sales YoY" onSort={onSort} sort={sort} /><SortHead className="text-right" column="operating_profit_yoy" label="OP YoY" onSort={onSort} sort={sort} />
          <SortHead className="text-right" column="er_annual" label="E[r] 参考" onSort={onSort} sort={sort} /><SortHead className="text-right" column="fair_value_gap_pct" label="FV乖離" onSort={onSort} sort={sort} />
          <SortHead column="next_earnings_date" label="next earnings" onSort={onSort} sort={sort} /><TableHead>data quality</TableHead><TableHead>portfolio / research</TableHead>
          {detail && <><TableHead className="text-right">EV/EBITDA</TableHead><TableHead className="text-right">P/S</TableHead><TableHead className="text-right">PCFR</TableHead><TableHead className="text-right">OCF yield</TableHead><TableHead className="text-right">equity ratio</TableHead><TableHead className="text-right">avg turnover</TableHead><TableHead className="text-right">margin short / ADV</TableHead><TableHead className="text-right">20d</TableHead><TableHead className="text-right">52w low</TableHead><TableHead className="text-right">sector RS%</TableHead></>}
        </TableRow></TableHeader>
        <TableBody>{projection.rows.map((row) => <TableRow key={row.ticker}>
          <TableCell><Link className="font-mono font-semibold hover:underline" to={`/securities/${row.ticker}`}>{row.ticker}</Link><span className="ml-2 text-muted-foreground">{row.name ?? EMPTY}</span></TableCell>
          <TableCell>{row.sector_33 ?? EMPTY}</TableCell><TableCell className="text-right font-mono">{row.market_cap_oku === null ? EMPTY : `${number(row.market_cap_oku, 0)} 億`}</TableCell>
          <TableCell className="text-right font-mono">{number(row.per_forward)}</TableCell><TableCell className="text-right font-mono">{number(row.per_trailing)}</TableCell><TableCell className="text-right font-mono">{number(row.normalized_per_3fy)}</TableCell><TableCell className="text-right font-mono">{number(row.pbr)}</TableCell>
          <TableCell className="text-right font-mono">{ratio(row.dividend_yield)}</TableCell><TableCell className="text-right font-mono">{ratio(row.net_cash_to_market_cap)}</TableCell><TableCell className="text-right font-mono">{ratio(row.fcf_yield)}</TableCell>
          <TableCell className="text-right font-mono">{ratio(row.sales_yoy)}</TableCell><TableCell className="text-right font-mono">{ratio(row.operating_profit_yoy)}</TableCell><TableCell className="text-right font-mono">{ratio(row.er_annual)}</TableCell><TableCell className="text-right font-mono">{percent(row.fair_value_gap_pct)}</TableCell>
          <TableCell>{row.next_earnings_date ?? EMPTY}</TableCell><TableCell><div className="flex max-w-64 flex-wrap gap-1">{row.data_quality_flags.length === 0 ? EMPTY : row.data_quality_flags.map((flag) => <Badge key={flag} variant="outline">{flag}</Badge>)}</div></TableCell>
          <TableCell><div className="flex gap-1"><Badge variant="secondary">{row.portfolio_state}</Badge>{row.has_research && <Badge variant="outline">research</Badge>}</div></TableCell>
          {detail && <><TableCell className="text-right font-mono">{number(row.ev_ebitda)}</TableCell><TableCell className="text-right font-mono">{number(row.p_s)}</TableCell><TableCell className="text-right font-mono">{number(row.pcfr)}</TableCell><TableCell className="text-right font-mono">{ratio(row.ocf_yield)}</TableCell><TableCell className="text-right font-mono">{ratio(row.equity_ratio)}</TableCell><TableCell className="text-right font-mono">{row.avg_turnover_oku === null ? EMPTY : `${number(row.avg_turnover_oku)} 億`}</TableCell><TableCell className="text-right font-mono">{number(row.margin_short_to_adv)}</TableCell><TableCell className="text-right font-mono">{ratio(row.price_change_20d)}</TableCell><TableCell className="text-right font-mono">{ratio(row.gap_from_52w_low)}</TableCell><TableCell className="text-right font-mono">{ratio(row.sector_relative_strength_percentile)}</TableCell></>}
        </TableRow>)}</TableBody>
      </Table>
      <div className="flex items-center justify-between border-t px-5 py-3 sm:px-6">
        <Button disabled={projection.page <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))} size="sm" variant="outline">前</Button>
        <span className="font-mono text-xs text-muted-foreground">{projection.page} / {projection.pageCount}</span>
        <Button disabled={projection.page >= projection.pageCount} onClick={() => setPage((value) => Math.min(projection.pageCount, value + 1))} size="sm" variant="outline">次</Button>
      </div>
    </SectionCard>
  )
}
