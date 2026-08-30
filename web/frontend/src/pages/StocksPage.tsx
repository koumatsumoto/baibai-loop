import { useEffect, useState } from 'react'
import { Link } from 'react-router'

import { fetchJson } from '../api/client'
import type { ScreeningView } from '../api/types'
import { AsOfBadge } from '../components/AsOfBadge'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageShell } from '../components/PageShell'
import { PageState } from '../components/PageState'
import { SectionCard } from '../components/SectionCard'
import { Badge } from '../components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../components/ui/table'
import { formatJstDateTime, formatNumber } from '../lib/format'

export function StocksPage() {
  const [data, setData] = useState<ScreeningView | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    fetchJson<ScreeningView>('/api/screening/latest').then(setData).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : 'Stocks を読み込めませんでした'))
  }, [])
  if (error) return <PageState message={error} title="Stocks read error" />
  if (!data) return <LoadingPage label="価値分析を読み込んでいます" />
  if (!data.run) return <PageState message="screening run publication がありません" title="Stocks" />
  const latestReviewSet = data.review_sets.at(-1) ?? null
  return (
    <PageShell lead="4つの価値評価法で構成した Review Set と、その後の人間判断を確認します。E[r] は参考見積りであり、Review Set の採否や順序には使いません。" meta={<div className="flex flex-wrap gap-4 font-mono text-xs text-muted-foreground"><AsOfBadge value={data.run.asof_date} /><span>分析 {data.run.analyzed_security_count.toLocaleString('ja-JP')} 銘柄</span></div>} title="Stocks">
      <SectionCard description="人間が Research / Skip を判断した発行済み記録" padded title="Research Triage">
        {data.research_triages.length === 0 ? <p className="text-sm text-muted-foreground">Research Triage はまだ publish されていません。</p> : <div className="flex flex-wrap gap-2">{data.research_triages.map((item) => <Link key={item.research_triage_id} to="/research-triage"><Badge variant="secondary">{item.research_triage_id}</Badge></Link>)}</div>}
      </SectionCard>
      <SectionCard description="Research Set の thesis を比較した資本配分判断" padded title="Capital Allocation Assessment">
        {data.capital_allocation_assessments.length === 0 ? <p className="text-sm text-muted-foreground">Capital Allocation Assessment はまだ publish されていません。</p> : <div className="grid gap-2">{data.capital_allocation_assessments.map((item) => <Link className="rounded-md border p-3 hover:bg-muted/40" key={item.capital_allocation_assessment_id} to={`/stocks/capital-allocation-assessments/${item.capital_allocation_assessment_id}`}><div className="flex flex-wrap items-center gap-2"><Badge>{item.result}</Badge><strong>{item.headline}</strong>{item.allocated_ticker && <span className="font-mono">{item.allocated_ticker}</span>}</div><p className="mt-1 text-xs text-muted-foreground">{item.as_of} · alternatives {item.alternative_count}</p></Link>)}</div>}
      </SectionCard>
      <SectionCard description="重複支持を優先し、20銘柄以内へ決定論的に構成" padded title="Review Set">
        {!latestReviewSet ? <p className="text-sm text-muted-foreground">Review Set はまだありません。</p> : <Table><TableHeader><TableRow><TableHead>順序</TableHead><TableHead>銘柄</TableHead><TableHead>名称</TableHead><TableHead>支持</TableHead><TableHead>評価法</TableHead></TableRow></TableHeader><TableBody>{latestReviewSet.entries.map((entry) => <TableRow key={entry.ticker}><TableCell>{entry.review_position}</TableCell><TableCell><Link className="font-mono font-semibold hover:underline" to={`/securities/${entry.ticker}`}>{entry.ticker}</Link></TableCell><TableCell>{entry.name}</TableCell><TableCell>{entry.support_count}</TableCell><TableCell>{entry.nominations.map((nomination) => String(nomination.valuation_approach_id ?? '')).join(' / ')}</TableCell></TableRow>)}</TableBody></Table>}
      </SectionCard>
      <SectionCard description="Review Set の入力となる全銘柄の観測値・導出値・参考見積り" padded title="Security Analysis">
        <Table><TableHeader><TableRow><TableHead>銘柄</TableHead><TableHead>名称</TableHead><TableHead>セクター</TableHead><TableHead className="text-right">PER</TableHead><TableHead className="text-right">PBR</TableHead><TableHead className="text-right">E[r] 参考</TableHead></TableRow></TableHeader><TableBody>{data.security_analyses.map((row) => <TableRow key={row.ticker}><TableCell><Link className="font-mono hover:underline" to={`/securities/${row.ticker}`}>{row.ticker}</Link></TableCell><TableCell>{row.name}</TableCell><TableCell>{row.sector_33}</TableCell><TableCell className="text-right font-mono">{row.per_trailing === null ? '—' : formatNumber(row.per_trailing, 2)}</TableCell><TableCell className="text-right font-mono">{row.pbr === null ? '—' : formatNumber(row.pbr, 2)}</TableCell><TableCell className="text-right font-mono">{row.er_annual === null ? '—' : `${formatNumber(row.er_annual * 100, 1)}%`}</TableCell></TableRow>)}</TableBody></Table>
        <p className="mt-3 text-xs text-muted-foreground">run {data.run.run_revision_id} · {formatJstDateTime(data.run.run_at)}</p>
      </SectionCard>
    </PageShell>
  )
}
