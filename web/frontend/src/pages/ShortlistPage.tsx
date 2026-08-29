import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router'

import { fetchJson } from '../api/client'
import type {
  CandidateRowView,
  ErLevelCalibrationContextView,
  MachineSelectionView,
  ScreeningView,
  SelectionRankedSetEntryView,
  ShortlistEntryView,
} from '../api/types'
import { AsOfBadge } from '../components/AsOfBadge'
import { InfoHint } from '../components/InfoHint'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageShell } from '../components/PageShell'
import { SectionCard } from '../components/SectionCard'
import { PageState } from '../components/PageState'
import { PctBadge } from '../components/PctBadge'
import { PortfolioStateBadge } from '../components/PortfolioStateBadge'
import { CountercaseBlock } from '../components/report/CountercaseBlock'
import { ReportToneBadge } from '../components/report/ReportToneBadge'
import { StaleBadge } from '../components/StaleBadge'
import { TradingViewButton } from '../components/TradingViewButton'
import { Badge } from '../components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../components/ui/table'
import { EMPTY, formatJstDateTime, formatNumber } from '../lib/format'
import { LABEL } from '../lib/labels'
import { buildShortlistComparison, rankDivergence, shortlistPermanentLossTone, summarize, type ShortlistComparisonRow } from '../lib/shortlist'
import { cn } from '../lib/utils'

// Research Gate narrative sections in render order. The risk-reward block leads because it decides
// whether a candidate earns a primary-research slot; the descriptive reads follow it.
const NARRATIVE_SECTIONS: readonly (readonly [keyof NarrativeText, string])[] = [
  ['upside', '上値の根拠'],
  ['downside', '下値の目安'],
  ['rr', 'リスクリワードが成立する理由'],
  ['why', 'なぜ安い可能性があるか'],
  ['temporary', '一時的な問題の可能性'],
  ['structural', '構造的な問題の可能性'],
  ['survive', '5 年間の事業・財務耐性'],
  ['unlock', '価値実現の仕組み'],
  ['catalyst', '再評価条件'],
  ['macro', 'マクロ環境の反映'],
  ['counter', '最も強い反対仮説'],
  ['research', '個別リサーチで確認する事項'],
  ['value', '一次リサーチ枠を使う価値'],
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
  upside: string | null
  downside: string | null
  rr: string | null
  catalyst: string | null
  macro: string | null
}

function yen(value: number | null, digits = 0) {
  return value === null ? EMPTY : `${formatNumber(value, digits)} 円`
}

function plain(value: number | null, digits = 1) {
  return value === null ? EMPTY : formatNumber(value, digits)
}

function FactRow({ label, children }: { label: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[9.5rem_1fr] gap-3 border-b py-1.5 text-sm last:border-b-0">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="text-right font-mono tabular-nums">{children}</dd>
    </div>
  )
}

function ErLevelContext({ calibration, row }: {
  calibration: ErLevelCalibrationContextView | null
  row: CandidateRowView | null
}) {
  const quintile = row?.er_level_quintile ?? null
  if (calibration === null || quintile === null) return null
  const horizon = calibration.horizons.find((item) => item.horizon === calibration.reference_horizon)
  const band = horizon?.bands.find((item) => item.quintile === quintile)
  const hurdle = row?.er_meets_8_5pct_band
    ? horizon?.bands.find((item) => item.band_id === 'er_gte_8_5pct')
    : null
  const stats = band?.bases.find((item) => item.basis === calibration.primary_realized_basis)?.ticker_equal
  const hurdleStats = hurdle?.bases.find((item) => item.basis === calibration.primary_realized_basis)?.ticker_equal
  if (!horizon || !band || !stats) return null
  return (
    <FactRow label={(
      <span className="inline-flex items-center gap-1">
        E[r] 履歴帯
        <InfoHint label="E[r] 履歴帯の注意">
          過去 panel の ticker-as-of 観測であり、この銘柄の予測ではありません。月次窓は重複します。total return は FY 実績配当を FY 末へ帰属させた近似です。trap は同じ cohort の母集団中央値に累積20pt以上劣後した割合です。
        </InfoHint>
      </span>
    )}>
      <span>Q{quintile} / 実現年率 median </span><PctBadge fraction value={stats.median} />
      <span>・q25 </span><PctBadge fraction value={stats.q25} />
      <span>・q10 </span><PctBadge fraction value={stats.q10} />
      <span>・trap </span><PctBadge fraction value={stats.trap_rate} />
      <span className="ml-1">({horizon.horizon}, total return, n={stats.n})</span>
      {hurdleStats && (
        <span className="block">E[r] ≥ 8.5% 帯: median <PctBadge fraction value={hurdleStats.median} />・q10 <PctBadge fraction value={hurdleStats.q10} />・trap <PctBadge fraction value={hurdleStats.trap_rate} /></span>
      )}
    </FactRow>
  )
}

function SupplyDemandCompact({ row }: { row: CandidateRowView | null }) {
  return (
    <span className="grid gap-0.5 whitespace-nowrap font-mono text-[10px] tabular-nums text-muted-foreground">
      <span>買/ADV {row?.margin_long_to_adv === null || row?.margin_long_to_adv === undefined ? EMPTY : `${plain(row.margin_long_to_adv, 2)}日`}</span>
      <span>売/ADV {row?.margin_short_to_adv === null || row?.margin_short_to_adv === undefined ? EMPTY : `${plain(row.margin_short_to_adv, 2)}日`}</span>
      <span>買率 <PctBadge fraction value={row?.margin_long_share ?? null} /></span>
      <span>26w <PctBadge fraction value={row?.margin_long_delta_26w ?? null} /></span>
      <span>制度 <PctBadge fraction value={row?.margin_std_long_share ?? null} /></span>
      <span>観測週 {row?.margin_week_end ?? EMPTY}</span>
    </span>
  )
}

function FvConvergenceBadge({ entry }: { entry: SelectionRankedSetEntryView | null }) {
  const convergence = entry?.fv_convergence
  if (!convergence || convergence.status === 'not_evaluable') {
    return <Badge className="text-[10px]" variant="outline">判定不能</Badge>
  }
  if (convergence.status === 'warning') {
    return <Badge className="bg-warning-surface text-[10px] text-warning-ink">全FVへ収束</Badge>
  }
  return <span className="text-muted-foreground">—</span>
}

function MachineFacts({ calibration, rankedSetEntry, row }: {
  calibration: ErLevelCalibrationContextView | null
  rankedSetEntry: SelectionRankedSetEntryView | null
  row: CandidateRowView | null
}) {
  if (rankedSetEntry === null && row === null) {
    return (
      <p className="rounded-md border border-dashed p-3 text-sm text-warning">
        この判断は機械座標を焼き込む前に publish されており、source run 世代も cache から prune 済みです。機械値は再現できないため narrative のみ表示しています。
      </p>
    )
  }
  const gap = rankedSetEntry?.fair_value_gap_pct ?? row?.fair_value_gap_pct ?? null
  const eventWarnings = rankedSetEntry?.event_warnings ?? []
  return (
    <dl>
      <FactRow label="screening 参考価格">{yen(rankedSetEntry?.market_price_yen ?? null, 1)}</FactRow>
      <FactRow label="FV アンカー / 乖離">
        {yen(rankedSetEntry?.fair_value_anchor_yen ?? row?.fair_value_anchor_yen ?? null)}
        {gap !== null && <span className="ml-2"><PctBadge value={gap} /></span>}
      </FactRow>
      <FactRow label="FVアンカーへの収束"><FvConvergenceBadge entry={rankedSetEntry} /></FactRow>
      <FactRow label="機械 E[r]"><PctBadge fraction value={row?.er_annual ?? null} /></FactRow>
      <ErLevelContext calibration={calibration} row={row} />
      <FactRow label="E[r] 分解（reversion〈価格回帰〉/ carry〈配当利回り + 株数縮小利回り〉）">
        <PctBadge fraction value={row?.er_reversion_annual ?? null} />
        <span className="mx-1 text-muted-foreground">/</span>
        <PctBadge fraction value={row?.er_carry_annual ?? null} />
      </FactRow>
      <FactRow label="PER(F / TTM / 3FY)">
        {plain(row?.per_forward ?? null)} / {plain(row?.per_trailing ?? null)} / {plain(row?.normalized_per_3fy ?? null)}
      </FactRow>
      <FactRow label="PBR">{plain(row?.pbr ?? null, 2)}</FactRow>
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
      <FactRow label="信用買残 / ADV">
        {row?.margin_long_to_adv === null || row?.margin_long_to_adv === undefined
          ? EMPTY
          : `${plain(row.margin_long_to_adv, 2)} 日分`}
      </FactRow>
      <FactRow label="信用売残 / ADV">
        {row?.margin_short_to_adv === null || row?.margin_short_to_adv === undefined
          ? EMPTY
          : `${plain(row.margin_short_to_adv, 2)} 日分`}
      </FactRow>
      <FactRow label="信用残の観測週">{row?.margin_week_end ?? EMPTY}</FactRow>
      <FactRow label="信用買残比率"><PctBadge fraction value={row?.margin_long_share ?? null} /></FactRow>
      <FactRow label="信用買残 26w変化"><PctBadge fraction value={row?.margin_long_delta_26w ?? null} /></FactRow>
      <FactRow label="制度信用買残比率"><PctBadge fraction value={row?.margin_std_long_share ?? null} /></FactRow>
      <FactRow label="売買代金">{row?.avg_turnover_oku === null || row?.avg_turnover_oku === undefined ? '—' : `${plain(row.avg_turnover_oku)} 億円/日`}</FactRow>
      <FactRow label="次回決算予定"><span className="text-right">{row?.next_earnings_date ?? LABEL.earningsTbd}</span></FactRow>
      <FactRow label="データ品質">
        {row && row.data_quality_flags.length > 0
          ? <span className="flex flex-wrap justify-end gap-1">{row.data_quality_flags.map((flag) => <Badge className="text-[10px]" key={flag} variant="outline">{flag}</Badge>)}</span>
          : <span className="text-muted-foreground">なし</span>}
      </FactRow>
      {eventWarnings.length > 0 && (
        <FactRow label="イベント警告">
          <span className="flex flex-wrap justify-end gap-1">{eventWarnings.map((warning) => <Badge className="text-[10px]" key={warning} variant="secondary">{warning}</Badge>)}</span>
        </FactRow>
      )}
    </dl>
  )
}

// Whether the human's ordering departs from the machine's, and by how much. The gate
// requires a written reason for a departure, so the surface names it instead of leaving
// the reader to diff two columns.
function DivergenceCell({ row }: { row: ShortlistComparisonRow }) {
  const divergence = rankDivergence(row)
  if (divergence === null) return <span className="text-muted-foreground">{EMPTY}</span>
  if (divergence === 0) return <span className="text-muted-foreground">一致</span>
  return (
    <span className={cn('font-medium', Math.abs(divergence) >= 5 && 'text-warning')}>
      {divergence > 0 ? `機械 +${divergence}` : `機械 ${divergence}`}
    </span>
  )
}

function ComparisonTable({ rows }: { rows: readonly ShortlistComparisonRow[] }) {
  return (
    <SectionCard description="暫定順位は深掘りの着手順の提案です。機械順位との乖離は narrative に理由があります。" padded title="候補比較">
        <Table className="text-sm">
          <TableHeader>
            <TableRow>
              <TableHead className="w-12 text-right">順位</TableHead>
              <TableHead className="w-24">銘柄コード</TableHead>
              <TableHead className="min-w-36">銘柄名</TableHead>
              <TableHead className="w-20 text-right">機械順位</TableHead>
              <TableHead className="w-20 text-right">乖離</TableHead>
              <TableHead className="w-20 text-right">E[r]</TableHead>
              <TableHead className="w-28 text-right">価格回帰 / 配当・株数縮小</TableHead>
              <TableHead className="w-20 text-right">FV乖離</TableHead>
              <TableHead className="w-24">FV収束</TableHead>
              <TableHead className="w-24">信用需給</TableHead>
              <TableHead className="min-w-56">リスクリワードが成立する理由</TableHead>
              <TableHead className="w-28">再評価条件</TableHead>
              <TableHead className="w-24">永久損失</TableHead>
              <TableHead className="w-24">保有/予約</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row) => (
              <TableRow key={row.ticker}>
                <TableCell className="text-right font-mono font-semibold tabular-nums">{row.rank ?? EMPTY}</TableCell>
                <TableCell><Link className="font-mono font-semibold underline-offset-4 hover:underline" to={`/securities/${row.ticker}`}>{row.ticker}</Link></TableCell>
                <TableCell className="max-w-44 truncate font-medium" title={row.name}>{row.name}</TableCell>
                <TableCell className="text-right font-mono tabular-nums">{row.machineRank ?? EMPTY}</TableCell>
                <TableCell className="text-right"><DivergenceCell row={row} /></TableCell>
                <TableCell className="text-right"><PctBadge fraction value={row.erAnnual} /></TableCell>
                <TableCell className="text-right font-mono text-xs tabular-nums">
                  <PctBadge fraction value={row.erReversionAnnual} />
                  <span className="mx-1 text-muted-foreground">/</span>
                  <PctBadge fraction value={row.erCarryAnnual} />
                </TableCell>
                <TableCell className="text-right"><PctBadge value={row.fairValueGapPct} /></TableCell>
                <TableCell><FvConvergenceBadge entry={row.rankedSetEntry} /></TableCell>
                <TableCell><SupplyDemandCompact row={row.row} /></TableCell>
                <TableCell className="text-muted-foreground" title={row.rr ?? undefined}>{summarize(row.rr) ?? EMPTY}</TableCell>
                <TableCell className="font-mono text-xs tabular-nums" title={row.catalyst ?? undefined}>
                  {row.catalystDate ?? <span className="text-muted-foreground">日付なし</span>}
                </TableCell>
                <TableCell>
                  {row.ploss === null
                    ? <span className="text-muted-foreground">{EMPTY}</span>
                    : <ReportToneBadge tone={shortlistPermanentLossTone(row.ploss)}>{row.ploss}</ReportToneBadge>}
                </TableCell>
                <TableCell><PortfolioStateBadge state={row.portfolioState} /></TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
    </SectionCard>
  )
}

function SelectedCard({ calibration, comparison }: {
  calibration: ErLevelCalibrationContextView | null
  comparison: ShortlistComparisonRow
}) {
  const entry: ShortlistEntryView = comparison.entry
  const narrative = entry.narrative
  return (
    <Card className="gap-0 overflow-hidden py-0 shadow-sm">
      <CardHeader className="flex flex-row items-center justify-between gap-3 border-b bg-muted/40 px-5 py-4">
        <div className="flex items-center gap-3">
          <span className="grid size-7 shrink-0 place-items-center rounded-full bg-foreground text-xs font-bold text-background">{comparison.rank ?? '—'}</span>
          <div>
            <CardTitle className="text-base">
              <Link className="font-mono underline-offset-4 hover:underline" to={`/securities/${entry.ticker}`}>{entry.ticker}</Link>
              <span className="ml-2 font-normal">{comparison.name}</span>
            </CardTitle>
            <CardDescription>{comparison.sector}</CardDescription>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {narrative && <ReportToneBadge tone={shortlistPermanentLossTone(narrative.ploss)}>永久損失(暫定): {narrative.ploss}</ReportToneBadge>}
          <TradingViewButton ticker={entry.ticker} />
        </div>
      </CardHeader>
      <CardContent className="grid gap-0 px-5 py-4 lg:grid-cols-[minmax(0,20rem)_1fr] lg:gap-6">
        <MachineFacts calibration={calibration} rankedSetEntry={comparison.rankedSetEntry} row={comparison.row} />
        <div className="mt-4 grid gap-3 lg:mt-0">
          {narrative
            ? NARRATIVE_SECTIONS.map(([key, heading]) => {
                const text = narrative[key]
                if (text === null || text === '') return null
                if (key === 'counter') return <CountercaseBlock key={key}>{text}</CountercaseBlock>
                return (
                  <div key={key}>
                    <h4 className="text-sm font-semibold text-accent-foreground/90">
                      {heading}
                      {key === 'catalyst' && comparison.catalystDate !== null && (
                        <span className="ml-2 font-mono text-xs font-normal text-muted-foreground">{comparison.catalystDate}</span>
                      )}
                    </h4>
                    <p className="text-sm text-muted-foreground">{text}</p>
                  </div>
                )
              })
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

export function ShortlistPage() {
  const [data, setData] = useState<ScreeningView | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetchJson<ScreeningView>('/api/screening/latest').then(setData).catch((reason: unknown) => {
      setError(reason instanceof Error ? reason.message : 'Shortlist を読み込めませんでした')
    })
  }, [])

  const shortlist = data?.shortlists[0] ?? null

  const selection = useMemo<MachineSelectionView | null>(() => {
    if (!data || !shortlist) return null
    return data.selections.find((item) => item.selection_id === shortlist.selection_id) ?? null
  }, [data, shortlist])

  const comparison = useMemo<readonly ShortlistComparisonRow[]>(() => {
    if (!shortlist) return []
    return buildShortlistComparison(shortlist, selection, data?.rows ?? [])
  }, [shortlist, selection, data])

  if (error) return <PageState message={error} title="Shortlist read error" />
  if (!data) return <LoadingPage label="Shortlist を読み込んでいます" />
  if (!shortlist) return <PageState message="shortlist はまだ publish されていません" title="Shortlist" />

  const rejected = shortlist.entries.filter((entry) => entry.decision === 'rejected')
  // 焼き込み済みの判断は selection が消えても機械値を持つ。警告は本当に何も無い場合だけ。
  const machineMissing =
    selection === null && comparison.every((item) => item.rankedSetEntry === null)

  return (
    <PageShell
      // The lead states what this cycle concluded. Promising "2〜4 銘柄を選んで進む" on a
      // cycle that selected none would contradict the card directly below it.
      lead={comparison.length === 0
        ? <>ranked_set を人間が review した結果。<strong>最終 buy 提案ではありません。</strong>このサイクルは深掘り候補を選ばず、shortlist が判断の記録になります。</>
        : <>ranked_set を人間が review して選んだ深掘り候補。<strong>最終 buy 提案ではありません。</strong>推奨 2〜4 銘柄を選んで個別リサーチへ進みます。</>}
      meta={(
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-xs text-muted-foreground tabular-nums">
          <AsOfBadge value={shortlist.as_of} />
          <span>{LABEL.published} {formatJstDateTime(shortlist.published_at)}</span>
          <span>選定 {comparison.length}・見送り {rejected.length}</span>
        </div>
      )}
      title="Shortlist レビュー面"
      width="reading"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
        <Badge className="font-mono text-[10px]" variant="secondary">{shortlist.shortlist_id}</Badge>
        <Link className="underline-offset-4 hover:text-foreground hover:underline" to="/stocks">Candidates を見る →</Link>
        {machineMissing && <span className="text-warning">source selection が最新 run に無いため機械値は非表示です</span>}
        {shortlist.unreadable_entries > 0 && (
          <span className="text-warning">{shortlist.unreadable_entries} 件の entry を読めないため表示から除いています</span>
        )}
        {data.run?.stale && <StaleBadge className="text-[10px]" detail={data.run.asof_date} />}
      </div>

      {/* Selecting nothing is a conclusion, not an empty page. Saying so keeps the
          reader from reading the missing comparison table as a load failure — but only
          when every entry was readable, since an unreadable selected entry empties the
          same table without anyone having concluded anything. */}
      {comparison.length === 0 && shortlist.unreadable_entries === 0 && (
        <SectionCard
          description="ranked_set を review したが、一次リサーチの枠を使う価値のある候補が無かったサイクル。銘柄ごとの見送り理由は下表に残る。"
          padded
          title="深掘り候補なし"
        >
          <p className="text-sm text-muted-foreground">次の ranked_set を待つか、条件を変えて再 screening する。</p>
        </SectionCard>
      )}

      {comparison.length > 0 && <ComparisonTable rows={comparison} />}

      <div className="grid gap-4">
        {comparison.map((row) => (
          <SelectedCard calibration={data.er_level_calibration ?? null} comparison={row} key={row.ticker} />
        ))}
      </div>

      {rejected.length > 0 && (
        <SectionCard description="review したが shortlist へ残さなかった理由" padded title="ranked_set から非選択">
            <Table className="text-sm">
              <TableHeader>
                <TableRow>
                  <TableHead className="w-28">銘柄コード</TableHead>
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
        </SectionCard>
      )}

      <p className="text-xs text-muted-foreground">
        機械 E[r]・FV アンカーは screening の機械見積り（reversion〈価格回帰〉+ carry〈配当利回り + 株数縮小利回り〉）で<strong>事実ではありません</strong>。上値・下値・リスクリワードは一次リサーチ前の暫定読みで、7 軸の永久損失評価と FV 確定は個別リサーチ（第 2 段階）で行います。
      </p>
    </PageShell>
  )
}
