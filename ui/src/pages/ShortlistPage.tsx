import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { ChartNoAxesCombined } from 'lucide-react'

import { fetchJson } from '../api/client'
import type {
  CandidateRowView,
  MachineSelectionView,
  ReviewedShortlistEntryView,
  ScreeningView,
} from '../api/types'
import { AppShell } from '../components/AppShell'
import { PctBadge } from '../components/PctBadge'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../components/ui/table'
import { Tooltip, TooltipContent, TooltipTrigger } from '../components/ui/tooltip'
import { cn, formatJstDateTime } from '../lib/utils'
import { tradingViewChartUrl } from '../lib/trading-view'

// OP3 narrative sections in render order (mirrors decision-cycle OP3 の判断項目).
const NARRATIVE_SECTIONS: readonly (readonly [keyof NarrativeText, string])[] = [
  ['why', 'なぜ安い可能性があるか'],
  ['temporary', '一時的な問題の可能性'],
  ['structural', '構造的な問題の可能性'],
  ['survive', '5 年間の財務耐性'],
  ['unlock', '株主価値が上がる条件'],
  ['counter', '最も強い反対仮説'],
  ['research', '個別リサーチで確認する事項'],
  ['value', '深掘りする価値'],
]

type NarrativeText = {
  why: string
  temporary: string
  structural: string
  survive: string
  unlock: string
  counter: string
  research: string
  value: string
}

const PLOSS_TONE: Record<string, string> = {
  低: 'bg-positive/15 text-positive',
  中低: 'bg-positive/15 text-positive',
  中: 'bg-amber-500/15 text-amber-700 dark:text-amber-400',
  要精査: 'bg-destructive/15 text-destructive',
  高: 'bg-destructive/15 text-destructive',
}

// audit_pool entry は型無し dict で届くので、機械値は明示的に coerce する。
function num(record: Record<string, unknown>, key: string): number | null {
  const value = record[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function strList(record: Record<string, unknown>, key: string): string[] {
  const value = record[key]
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

function fvGapPct(audit: Record<string, unknown> | null): number | null {
  if (audit === null) return null
  const fv = num(audit, 'fair_value_anchor_yen')
  const px = num(audit, 'market_price_yen')
  return fv !== null && px !== null && px !== 0 ? (fv / px - 1) * 100 : null
}

function yen(value: number | null, digits = 0) {
  return value === null ? '—' : `${value.toLocaleString('ja-JP', { maximumFractionDigits: digits })} 円`
}

function plain(value: number | null, digits = 1) {
  return value === null ? '—' : value.toLocaleString('ja-JP', { maximumFractionDigits: digits })
}

function FactRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[9.5rem_1fr] gap-3 border-b py-1.5 text-sm last:border-b-0">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="text-right font-mono tabular-nums">{children}</dd>
    </div>
  )
}

function MachineFacts({ audit, row }: { audit: Record<string, unknown> | null; row: CandidateRowView | null }) {
  if (audit === null && row === null) {
    return (
      <p className="rounded-md border border-dashed p-3 text-sm text-amber-700 dark:text-amber-400">
        この shortlist の source run 世代は cache から prune 済みです。機械値は再現できないため narrative のみ表示しています。
      </p>
    )
  }
  const gap = fvGapPct(audit)
  const eventWarnings = audit ? strList(audit, 'event_warnings') : []
  return (
    <dl>
      <FactRow label="screening 参考価格">{yen(audit ? num(audit, 'market_price_yen') : null, 1)}</FactRow>
      <FactRow label="FV アンカー / 乖離">
        {yen(audit ? num(audit, 'fair_value_anchor_yen') : null)}
        {gap !== null && <span className="ml-2"><PctBadge value={gap} /></span>}
      </FactRow>
      <FactRow label="機械 E[r]"><PctBadge fraction value={row?.er_annual ?? null} /></FactRow>
      <FactRow label="E[r] 分解 (rev / carry)">
        <PctBadge fraction value={row?.er_reversion_annual ?? null} />
        <span className="mx-1 text-muted-foreground">/</span>
        <PctBadge fraction value={row?.er_carry_annual ?? null} />
      </FactRow>
      <FactRow label="PER(F) / PBR">{plain(row?.per_forward ?? null)} / {plain(row?.pbr ?? null, 2)}</FactRow>
      <FactRow label="売上 / 営業益 YoY">
        <PctBadge fraction value={row?.sales_yoy ?? null} />
        <span className="mx-1 text-muted-foreground">/</span>
        <PctBadge fraction value={row?.operating_profit_yoy ?? null} />
      </FactRow>
      <FactRow label="自己資本比率 / Net cash">
        <PctBadge fraction value={row?.equity_ratio ?? null} />
        <span className="mx-1 text-muted-foreground">/</span>
        <PctBadge fraction value={row?.net_cash_to_market_cap ?? null} />
      </FactRow>
      <FactRow label="値位置 (20d / 52w安値)">
        <PctBadge fraction value={row?.price_change_20d ?? null} />
        <span className="mx-1 text-muted-foreground">/</span>
        <PctBadge fraction value={row?.gap_from_52w_low ?? null} />
      </FactRow>
      <FactRow label="売買代金">{row?.avg_turnover_oku === null || row?.avg_turnover_oku === undefined ? '—' : `${plain(row.avg_turnover_oku)} 億円/日`}</FactRow>
      <FactRow label="次回決算予定"><span className="text-right">{row?.next_earnings_date ?? '未定/JPX未公表'}</span></FactRow>
      <FactRow label="データ品質">
        {row && row.data_quality_flags.length > 0
          ? <span className="flex flex-wrap justify-end gap-1">{row.data_quality_flags.map((flag) => <Badge className="text-[10px]" key={flag} variant="outline">{flag}</Badge>)}</span>
          : <span className="text-muted-foreground">なし</span>}
      </FactRow>
      {eventWarnings.length > 0 && (
        <FactRow label="event warning">
          <span className="flex flex-wrap justify-end gap-1">{eventWarnings.map((warning) => <Badge className="text-[10px]" key={warning} variant="secondary">{warning}</Badge>)}</span>
        </FactRow>
      )}
    </dl>
  )
}

function SelectedCard({
  index,
  entry,
  audit,
  row,
}: {
  index: number
  entry: ReviewedShortlistEntryView
  audit: Record<string, unknown> | null
  row: CandidateRowView | null
}) {
  const narrative = entry.narrative
  const name = row?.name ?? entry.ticker
  const sector = narrative?.sector_label ?? row?.sector_33 ?? '—'
  return (
    <Card className="gap-0 overflow-hidden py-0 shadow-sm">
      <CardHeader className="flex flex-row items-center justify-between gap-3 border-b bg-muted/40 px-5 py-4">
        <div className="flex items-center gap-3">
          <span className="grid size-7 shrink-0 place-items-center rounded-full bg-foreground text-xs font-bold text-background">{index}</span>
          <div>
            <CardTitle className="text-base">
              <Link className="font-mono underline-offset-4 hover:underline" to={`/securities/${entry.ticker}`}>{entry.ticker}</Link>
              <span className="ml-2 font-normal">{name}</span>
            </CardTitle>
            <CardDescription>{sector}</CardDescription>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {narrative && <Badge className={cn('font-semibold', PLOSS_TONE[narrative.ploss] ?? 'bg-amber-500/15 text-amber-700')}>永久損失(暫定): {narrative.ploss}</Badge>}
          <Tooltip>
            <TooltipTrigger asChild>
              <Button asChild size="icon-sm" variant="ghost">
                <a aria-label={`${entry.ticker} の TradingView`} href={tradingViewChartUrl(entry.ticker)} rel="noopener noreferrer" target="_blank"><ChartNoAxesCombined aria-hidden="true" /></a>
              </Button>
            </TooltipTrigger>
            <TooltipContent>TradingView でチャートを開く</TooltipContent>
          </Tooltip>
        </div>
      </CardHeader>
      <CardContent className="grid gap-0 px-5 py-4 lg:grid-cols-[minmax(0,20rem)_1fr] lg:gap-6">
        <MachineFacts audit={audit} row={row} />
        <div className="mt-4 grid gap-3 lg:mt-0">
          {narrative
            ? NARRATIVE_SECTIONS.map(([key, heading]) => (
                <div key={key}>
                  <h4 className="text-sm font-semibold text-accent-foreground/90">{heading}</h4>
                  <p className="text-sm text-muted-foreground">{narrative[key]}</p>
                </div>
              ))
            : <p className="text-sm text-muted-foreground">{entry.reason}</p>}
          {narrative && (
            <div className="rounded-md border bg-muted/30 p-2.5">
              <span className="text-xs font-semibold text-muted-foreground">暫定判断</span>
              <p className="text-sm font-medium">{narrative.prov}</p>
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  )
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

export function ShortlistPage() {
  const [data, setData] = useState<ScreeningView | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetchJson<ScreeningView>('/api/screening/latest').then(setData).catch((reason: unknown) => {
      setError(reason instanceof Error ? reason.message : 'Shortlist を読み込めませんでした')
    })
  }, [])

  const shortlist = data?.reviewed_shortlists[0] ?? null

  const selection = useMemo<MachineSelectionView | null>(() => {
    if (!data || !shortlist) return null
    return data.selections.find((item) => item.selection_id === shortlist.selection_id) ?? null
  }, [data, shortlist])

  const auditByTicker = useMemo(() => {
    const map = new Map<string, Record<string, unknown>>()
    for (const entry of selection?.audit_pool ?? []) {
      const ticker = entry.ticker
      if (typeof ticker === 'string') map.set(ticker, entry)
    }
    return map
  }, [selection])

  const rowByTicker = useMemo(() => {
    const map = new Map<string, CandidateRowView>()
    for (const row of data?.rows ?? []) map.set(row.ticker, row)
    return map
  }, [data])

  if (error) return <PageState message={error} title="Shortlist read error" />
  if (!data) return <PageState message="Shortlist を読み込んでいます…" title="Shortlist" />
  if (!shortlist) return <PageState message="reviewed shortlist はまだ publish されていません" title="Shortlist" />

  const selected = shortlist.entries.filter((entry) => entry.decision === 'selected')
  const rejected = shortlist.entries.filter((entry) => entry.decision === 'rejected')
  const machineMissing = selection === null

  return (
    <>
      <AppShell />
      <main className="mx-auto grid max-w-6xl gap-5 px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
        <header className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">Shortlist レビュー面</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              audit pool から人間 review した深掘り候補。<strong>最終 buy 提案ではありません。</strong>推奨 2〜4 銘柄を選んで個別リサーチへ進みます。
            </p>
          </div>
          <dl className="grid grid-cols-2 gap-x-8 gap-y-2 sm:grid-cols-4">
            {[
              ['基準 (AS OF)', shortlist.as_of],
              ['公表 (PUBLISHED)', formatJstDateTime(shortlist.published_at)],
              ['SELECTED', String(selected.length)],
              ['REJECTED', String(rejected.length)],
            ].map(([label, value]) => (
              <div key={label}>
                <dt className="text-[10px] font-semibold tracking-wider text-muted-foreground">{label}</dt>
                <dd className="mt-1 font-mono text-sm font-medium tabular-nums">{value}</dd>
              </div>
            ))}
          </dl>
        </header>

        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
          <Badge className="font-mono text-[10px]" variant="secondary">{shortlist.shortlist_id}</Badge>
          <Link className="underline-offset-4 hover:text-foreground hover:underline" to="/screening">全通過 candidates を見る →</Link>
          {machineMissing && <span className="text-amber-700 dark:text-amber-400">source selection が最新 run に無いため機械値は非表示です</span>}
          {data.run?.stale && <Badge className="text-[10px]" variant="outline">run stale ({data.run.asof_date})</Badge>}
        </div>

        <div className="grid gap-4">
          {selected.map((entry, index) => (
            <SelectedCard
              audit={machineMissing ? null : auditByTicker.get(entry.ticker) ?? null}
              entry={entry}
              index={index + 1}
              key={entry.ticker}
              row={machineMissing ? null : rowByTicker.get(entry.ticker) ?? null}
            />
          ))}
        </div>

        {rejected.length > 0 && (
          <Card className="gap-3 py-5 shadow-sm">
            <CardHeader className="px-5">
              <CardTitle className="text-base">audit pool から非選択</CardTitle>
              <CardDescription>review したが shortlist へ残さなかった理由</CardDescription>
            </CardHeader>
            <CardContent className="px-5">
              <Table className="text-sm">
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-28">ticker</TableHead>
                    <TableHead>非選択理由</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rejected.map((entry) => (
                    <TableRow key={entry.ticker}>
                      <TableCell><Link className="font-mono underline-offset-4 hover:underline" to={`/securities/${entry.ticker}`}>{entry.ticker}</Link></TableCell>
                      <TableCell className="text-muted-foreground">{entry.reason}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        )}

        <p className="text-xs text-muted-foreground">
          機械 E[r]・FV アンカーは screening の機械見積り（reversion + carry）で<strong>事実ではありません</strong>。永久損失は第 1 段階の暫定読みで、7 軸の本評価は個別リサーチ（第 2 段階）で行います。
        </p>
      </main>
    </>
  )
}
