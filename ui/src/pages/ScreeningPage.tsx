import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { fetchJson } from '../api/client'
import type { CandidateRowView, ScreeningView } from '../api/types'
import { AppShell } from '../components/AppShell'
import { PctBadge } from '../components/PctBadge'

type SortDirection = 'asc' | 'desc'
type SortKey = keyof CandidateRowView

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
  return <th className={right ? 'right' : undefined}><button className={active ? 'sort-button active' : 'sort-button'} type="button" onClick={() => onSort(column)}>{label}<span>{active ? direction === 'asc' ? '↑' : '↓' : '↕'}</span></button></th>
}

function Metric({ value, digits = 2 }: { value: number | null; digits?: number }) {
  return value === null ? <span className="muted">—</span> : <span className="numeric">{value.toLocaleString('ja-JP', { maximumFractionDigits: digits })}</span>
}

export function ScreeningPage() {
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
      setError(reason instanceof Error ? reason.message : 'Screening を読み込めませんでした')
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
      if (heldOnly && !row.held) return false
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

  if (error) return <><AppShell /><main className="page page--message"><p className="eyebrow">READ ERROR</p><h1>Screening</h1><p>{error}</p></main></>
  if (!data) return <><AppShell /><main className="page page--loading"><p className="eyebrow">SCREENING</p><h1>候補を読み込み中…</h1></main></>
  if (!data.run) return <><AppShell /><main className="page page--message"><p className="eyebrow">SCREENING</p><h1>実行結果がありません</h1><p>screening 実行結果がありません（records/02-candidates が空）</p></main></>

  const visibleRows = showAll ? rows : rows.slice(0, 500)
  return (
    <>
      <AppShell />
      <main className="page data-page">
        <section className="data-heading">
          <div><p className="eyebrow">LATEST SCREENING</p><h1>候補を、比較する。</h1></div>
          <dl className="run-facts"><div><dt>RUN</dt><dd>{data.run.run_date}</dd></div><div><dt>AS OF</dt><dd>{data.run.asof_date}</dd></div><div><dt>UNIVERSE</dt><dd>{data.run.universe_size.toLocaleString('ja-JP')}</dd></div><div><dt>CANDIDATES</dt><dd>{data.run.candidate_count.toLocaleString('ja-JP')}</dd></div></dl>
        </section>
        <p className="source-path">{data.run.source_path}</p>

        <section className="filters" aria-label="screening filters">
          <label className="filter-wide"><span>銘柄</span><input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="ticker 前方一致 / 銘柄名" /></label>
          <label><span>sector</span><select value={sector} onChange={(event) => setSector(event.target.value)}><option value="">すべて</option>{sectors.map((item) => <option key={item}>{item}</option>)}</select></label>
          <label><span>PER ≤</span><input inputMode="decimal" value={perMax} onChange={(event) => setPerMax(event.target.value)} placeholder="無条件" /></label>
          <label><span>PBR ≤</span><input inputMode="decimal" value={pbrMax} onChange={(event) => setPbrMax(event.target.value)} placeholder="無条件" /></label>
          <label><span>配当 ≥ %</span><input inputMode="decimal" value={dividendMin} onChange={(event) => setDividendMin(event.target.value)} placeholder="無条件" /></label>
          <label className="check-filter"><input type="checkbox" checked={heldOnly} onChange={(event) => setHeldOnly(event.target.checked)} /><span>保有のみ</span></label>
          <label className="check-filter"><input type="checkbox" checked={researchOnly} onChange={(event) => setResearchOnly(event.target.checked)} /><span>research 有り</span></label>
        </section>

        <div className="results-line"><strong>{rows.length.toLocaleString('ja-JP')} 件</strong><span>default: E[r] 降順 / null は末尾</span>{!showAll && rows.length > 500 && <span>先頭 500 件を表示</span>}</div>
        <div className="table-wrap screening-table"><table><thead><tr>
          <SortHeader label="ticker" column="ticker" sortKey={sortKey} direction={direction} onSort={onSort} />
          <SortHeader label="name" column="name" sortKey={sortKey} direction={direction} onSort={onSort} />
          <SortHeader label="sector" column="sector_33" sortKey={sortKey} direction={direction} onSort={onSort} />
          <SortHeader label="時価総額(億)" column="market_cap_oku" sortKey={sortKey} direction={direction} onSort={onSort} right />
          <SortHeader label="PER" column="per_trailing" sortKey={sortKey} direction={direction} onSort={onSort} right />
          <SortHeader label="PER(F)" column="per_forward" sortKey={sortKey} direction={direction} onSort={onSort} right />
          <SortHeader label="PBR" column="pbr" sortKey={sortKey} direction={direction} onSort={onSort} right />
          <SortHeader label="配当" column="dividend_yield" sortKey={sortKey} direction={direction} onSort={onSort} right />
          <SortHeader label="E[r]" column="er_annual" sortKey={sortKey} direction={direction} onSort={onSort} right />
          <SortHeader label="Net cash" column="net_cash_to_market_cap" sortKey={sortKey} direction={direction} onSort={onSort} right />
          <SortHeader label="FCF yield" column="fcf_yield" sortKey={sortKey} direction={direction} onSort={onSort} right />
          <SortHeader label="20d" column="price_change_20d" sortKey={sortKey} direction={direction} onSort={onSort} right />
          <SortHeader label="52w low" column="gap_from_52w_low" sortKey={sortKey} direction={direction} onSort={onSort} right />
          <SortHeader label="決算予定" column="next_earnings_date" sortKey={sortKey} direction={direction} onSort={onSort} />
          <SortHeader label="保有" column="held" sortKey={sortKey} direction={direction} onSort={onSort} />
          <SortHeader label="research" column="has_research" sortKey={sortKey} direction={direction} onSort={onSort} />
        </tr></thead><tbody>{visibleRows.map((row) => <tr key={row.ticker}>
          <td><Link className="ticker-link" to={`/securities/${row.ticker}`}>{row.ticker}</Link></td><td>{row.name ?? '—'}</td><td>{row.sector_33 ?? '—'}</td>
          <td className="right"><Metric value={row.market_cap_oku} digits={0} /></td><td className="right"><Metric value={row.per_trailing} /></td><td className="right"><Metric value={row.per_forward} /></td><td className="right"><Metric value={row.pbr} /></td>
          <td className="right"><PctBadge value={row.dividend_yield} fraction /></td><td className="right"><PctBadge value={row.er_annual} fraction /></td><td className="right"><PctBadge value={row.net_cash_to_market_cap} fraction /></td><td className="right"><PctBadge value={row.fcf_yield} fraction /></td><td className="right"><PctBadge value={row.price_change_20d} fraction /></td><td className="right"><PctBadge value={row.gap_from_52w_low} fraction /></td>
          <td>{row.next_earnings_date ?? '—'}</td><td><span className={row.held ? 'flag flag--on' : 'flag'}>{row.held ? '保有' : '—'}</span></td><td><span className={row.has_research ? 'flag flag--on' : 'flag'}>{row.has_research ? '有' : '—'}</span></td>
        </tr>)}</tbody></table></div>
        {!showAll && rows.length > 500 && <button className="show-all" type="button" onClick={() => setShowAll(true)}>全 {rows.length.toLocaleString('ja-JP')} 件を表示</button>}
      </main>
    </>
  )
}
