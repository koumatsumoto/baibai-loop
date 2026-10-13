import { useMemo, type ReactNode } from 'react'
import { Link, useLocation, useNavigate } from 'react-router'
import { Search } from 'lucide-react'
import type { ReviewSetEntryView, ReviewSetView } from '../../api/types'
import { SectionCard } from '../../components/SectionCard'
import { Badge } from '../../components/ui/badge'
import { Button } from '../../components/ui/button'
import { Input } from '../../components/ui/input'
import { EMPTY, formatJstDateTime, formatNumber } from '../../lib/format'
import { parseOptionalNumber, projectSecurityRows, SECURITY_PAGE_SIZE, valuationApproachLabel, type SecurityFilters, type SecuritySearchRow, type SecuritySort, type SecuritySortKey } from '../../lib/stocks'

function number(value: number | null | undefined): string { return value == null ? EMPTY : formatNumber(value, 2) }
function ratio(value: number | null | undefined): string { return value == null ? '未観測' : `${formatNumber(value * 100, 1)}%` }
function FilterLabel({ title, children }: { title: string; children: ReactNode }) {
  return <label className="grid gap-1"><span className="text-xs font-medium text-muted-foreground">{title}</span>{children}</label>
}
const SORT_LABELS: Record<SecuritySortKey, string> = {
  ticker: '銘柄コード', name: '会社名', sector_33: '業種', market_cap_oku: '時価総額',
  per_forward: '予想PER', per_trailing: '実績PER', normalized_per_3fy: '3FY平均EPSベースPER', pbr: 'PBR',
  dividend_yield: '配当利回り', net_cash_to_market_cap: 'ネットキャッシュ比率', fcf_yield: 'FCF利回り',
  sales_yoy: '売上成長率', operating_profit_yoy: '営業益成長率', er_annual: '機械E[r]',
  fair_value_gap_pct: '機械anchor乖離', next_earnings_date: '次の決算',
}

export function SecuritySearchItem({ row, candidate }: { row: SecuritySearchRow; candidate?: ReviewSetEntryView }) {
  const { pathname, search } = useLocation()
  const origin = { exploration: pathname + search }
  const analysis = 'er_annual' in row ? row : null
  return <li className="grid gap-3 px-5 py-4 sm:px-6 lg:grid-cols-[minmax(14rem,2fr)_minmax(20rem,3fr)_minmax(14rem,2fr)] lg:items-start">
    <div className="grid min-w-0 gap-1.5">
      <Link className="font-medium underline-offset-4 hover:underline" state={origin} to={`/securities/${row.ticker}`}>{row.name ?? '名称未確認'} <span className="font-mono text-xs text-muted-foreground">{row.ticker}</span></Link>
      <p className="text-xs text-muted-foreground">{row.sector_33 ?? '業種未確認'}{analysis?.market_cap_oku != null && ` · 時価総額 ${formatNumber(analysis.market_cap_oku, 0)}億円`}</p>
      <div className="flex flex-wrap gap-1">{candidate?.nominations.map(n => <Badge key={n.valuation_approach_id} variant="outline">{valuationApproachLabel(n.valuation_approach_id, n.rank)}</Badge>)}{row.portfolio_state !== 'unheld' && <Badge variant="secondary">{row.portfolio_state === 'held' || row.portfolio_state === 'held_and_reserved' ? '保有' : '予約'}</Badge>}{row.has_research && <Badge variant="secondary">企業調査あり</Badge>}</div>
    </div>
    {analysis ? <dl className="grid grid-cols-2 gap-x-3 gap-y-2 text-xs sm:grid-cols-3">
      {[
        ['PER 予想 / 実績', `${number(analysis.per_forward)} / ${number(analysis.per_trailing)}`],
        ['PBR', number(analysis.pbr)], ['配当利回り', ratio(analysis.dividend_yield)],
        ['機械E[r]（%/年）', ratio(analysis.er_annual)], ['FCF利回り', ratio(analysis.fcf_yield)], ['Net cash / 時価総額', ratio(analysis.net_cash_to_market_cap)],
      ].map(([label, value]) => <div key={label}><dt className="text-muted-foreground">{label}</dt><dd className="mt-1 font-mono tabular-nums">{value}</dd></div>)}
    </dl> : <p className="text-sm text-muted-foreground">保存済み外部参考評価 · 機械分析なし</p>}
    <div className="grid gap-1.5 text-xs text-muted-foreground">
      {analysis?.next_earnings_date && <p>次の決算 {analysis.next_earnings_date}</p>}
      {analysis && analysis.data_quality_flags.length > 0 && <p className="break-words">入力の注意 {analysis.data_quality_flags.length}件（詳細で確認）</p>}
      <Link className="text-foreground underline underline-offset-4" state={origin} to={`/securities/${row.ticker}`}>評価・市場指標・外部参考を読む →</Link>
    </div>
  </li>
}

export function SecurityAnalysisSection({ rows, runAsOf, externalCutoff, reviewSet }: { rows: SecuritySearchRow[]; runAsOf: string | null; externalCutoff?: string | null; reviewSet?: ReviewSetView | null }) {
  const sectors = useMemo(() => [...new Set(rows.flatMap(row => row.sector_33 === null ? [] : [row.sector_33]))].sort((a, b) => a.localeCompare(b, 'ja-JP')), [rows])
  const candidates = useMemo(() => new Map(reviewSet?.entries.map(entry => [entry.ticker, entry]) ?? []), [reviewSet])
  const { pathname, search } = useLocation()
  const navigate = useNavigate()
  const params = useMemo(() => new URLSearchParams(search), [search])
  const query = params.get('q') ?? ''
  const sector = params.get('sector') ?? 'all'
  const perMax = params.get('per') ?? ''
  const pbrMax = params.get('pbr') ?? ''
  const dividendMin = params.get('dividend') ?? ''
  const heldOnly = params.get('held') === '1'
  const researchOnly = params.get('researched') === '1'
  const candidatesOnly = params.get('candidates') === '1'
  const sort: SecuritySort = { key: (params.get('sort') ?? 'ticker') as SecuritySortKey, direction: params.get('direction') === 'desc' ? 'desc' : 'asc' }
  const page = Math.floor(parseOptionalNumber(params.get('page') ?? '1') ?? 1)
  const filters: SecurityFilters = { query, sector, perMax: parseOptionalNumber(perMax), pbrMax: parseOptionalNumber(pbrMax), dividendMinPct: parseOptionalNumber(dividendMin), heldOnly, researchOnly }
  const projection = projectSecurityRows(candidatesOnly ? rows.filter(row => candidates.has(row.ticker)) : rows, filters, sort, page)
  function updateParam(name: string, value: string) {
    const next = new URLSearchParams(params)
    if (value) next.set(name, value)
    else next.delete(name)
    if (name !== 'page') next.delete('page')
    navigate({ pathname, search: next.size ? `?${next}` : '' }, { replace: true })
  }
  const start = projection.filteredTotal === 0 ? 0 : (projection.page - 1) * SECURITY_PAGE_SIZE + 1
  const end = Math.min(projection.page * SECURITY_PAGE_SIZE, projection.filteredTotal)
  return <SectionCard title="銘柄検索" description={`機械分析の基準 ${runAsOf ?? '未発行'}。候補の理由は選定時のApproachです。E[r]による採否ではありません。`}>
    <div className="grid gap-3 border-b px-5 py-4 sm:px-6">
      <div className="grid gap-3 sm:grid-cols-2">
        <FilterLabel title="銘柄コード・会社名"><div className="relative"><Search aria-hidden="true" className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" /><Input aria-label="tickerまたは会社名を検索" className="pl-8" onChange={event => updateParam('q', event.target.value)} placeholder="銘柄コードの前方 / 会社名の部分一致" value={query} /></div></FilterLabel>
        <FilterLabel title="業種"><select className="h-9 rounded-md border bg-background px-3 text-sm" onChange={event => updateParam('sector', event.target.value === 'all' ? '' : event.target.value)} value={sector}><option value="all">すべての業種</option>{sectors.map(item => <option key={item}>{item}</option>)}</select></FilterLabel>
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-2 text-sm"><label className="inline-flex items-center gap-2"><input checked={candidatesOnly} onChange={event => updateParam('candidates', event.target.checked ? '1' : '')} type="checkbox" />機械の候補のみ</label><label className="inline-flex items-center gap-2"><input checked={heldOnly} onChange={event => updateParam('held', event.target.checked ? '1' : '')} type="checkbox" />保有のみ</label><label className="inline-flex items-center gap-2"><input checked={researchOnly} onChange={event => updateParam('researched', event.target.checked ? '1' : '')} type="checkbox" />企業調査あり</label></div>
      <details><summary className="cursor-pointer text-sm">数値条件・並べ替え</summary><div className="mt-3 grid gap-3 sm:grid-cols-3 lg:grid-cols-5">
        <FilterLabel title="実績PER ≤"><Input inputMode="decimal" onChange={event => updateParam('per', event.target.value)} value={perMax} /></FilterLabel>
        <FilterLabel title="PBR ≤"><Input inputMode="decimal" onChange={event => updateParam('pbr', event.target.value)} value={pbrMax} /></FilterLabel>
        <FilterLabel title="配当利回り ≥ %"><Input inputMode="decimal" onChange={event => updateParam('dividend', event.target.value)} value={dividendMin} /></FilterLabel>
        <FilterLabel title="並べ替え"><select className="h-9 rounded-md border bg-background px-3 text-sm" onChange={event => updateParam('sort', event.target.value)} value={sort.key}>{Object.entries(SORT_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></FilterLabel>
        <FilterLabel title="順序"><select className="h-9 rounded-md border bg-background px-3 text-sm" onChange={event => updateParam('direction', event.target.value)} value={sort.direction}><option value="asc">昇順</option><option value="desc">降順</option></select></FilterLabel>
      </div></details>
      <p aria-live="polite" className="text-xs text-muted-foreground">{start}–{end} / {projection.filteredTotal}件</p>
      {externalCutoff && <p className="text-xs text-muted-foreground">外部観測の読取時点 {formatJstDateTime(externalCutoff)} JST。機械分析なしの銘柄は数値条件では抽出しません。</p>}
    </div>
    {projection.filteredTotal === 0 ? <p className="px-5 py-8 text-sm text-muted-foreground">条件に合う銘柄はありません。</p> : <ul className="divide-y">{projection.rows.map(row => <SecuritySearchItem candidate={candidates.get(row.ticker)} key={row.ticker} row={row} />)}</ul>}
    <div className="flex items-center justify-between gap-2 border-t px-5 py-3 sm:px-6"><Button disabled={projection.page <= 1} onClick={() => updateParam('page', String(projection.page - 1))} size="sm" variant="outline">前</Button><span aria-live="polite" className="text-xs text-muted-foreground">{start}–{end} / {projection.filteredTotal}件 · {projection.page}/{projection.pageCount}</span><Button disabled={projection.page >= projection.pageCount} onClick={() => updateParam('page', String(projection.page + 1))} size="sm" variant="outline">次</Button></div>
  </SectionCard>
}
