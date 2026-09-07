import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router'

import { ApiError, fetchJson } from '../api/client'
import type { SecurityAnalysisRowView, SecurityDetailView } from '../api/types'
import { AsOfBadge } from '../components/AsOfBadge'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageShell } from '../components/PageShell'
import { SectionCard } from '../components/SectionCard'
import { PageState } from '../components/PageState'
import { PctBadge } from '../components/PctBadge'
import { TradingViewButton } from '../components/TradingViewButton'
import { YenAmount } from '../components/YenAmount'
import { Badge } from '../components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../components/ui/table'
import { LABEL } from '../lib/labels'
import { cn } from '../lib/utils'

function Field({ label, children, className }: { label: string; children: React.ReactNode; className?: string }) {
  return (
    <div className={cn('grid gap-1', className)}>
      <dt className="text-xs font-medium text-muted-foreground">{label}</dt>
      <dd className="text-sm font-medium">{children}</dd>
    </div>
  )
}

function FractionMetric({ value }: { value: number | null }) {
  return <PctBadge fraction value={value} />
}

function ScreeningMetrics({ row }: { row: SecurityAnalysisRowView }) {
  return (
    <dl className="grid gap-6 sm:grid-cols-2 lg:grid-cols-5">
      <Field label="時価総額"><span className="font-mono tabular-nums">{row.market_cap_oku === null ? '—' : `${row.market_cap_oku.toLocaleString('ja-JP')} 億円`}</span></Field>
      <Field label="FV アンカー / 乖離"><span className="font-mono tabular-nums">{row.fair_value_anchor_yen === null ? '—' : `${row.fair_value_anchor_yen.toLocaleString('ja-JP')} 円`} / <PctBadge value={row.fair_value_gap_pct} /></span></Field>
      <Field label="PER / forward"><span className="font-mono tabular-nums">{row.per_trailing ?? '—'} / {row.per_forward ?? '—'}</span></Field>
      <Field label="PBR"><span className="font-mono tabular-nums">{row.pbr ?? '—'}</span></Field>
      <Field label="EV/EBITDA"><span className="font-mono tabular-nums">{row.ev_ebitda ?? '—'}</span></Field>
      <Field label="P/S"><span className="font-mono tabular-nums">{row.p_s ?? '—'}</span></Field>
      <Field label="PCFR"><span className="font-mono tabular-nums">{row.pcfr ?? '—'}</span></Field>
      <Field label="配当利回り"><FractionMetric value={row.dividend_yield} /></Field>
      <Field label="E[r] (rev/carry)"><span className="font-mono tabular-nums"><FractionMetric value={row.er_annual} /> (<FractionMetric value={row.er_reversion_annual} /> / <FractionMetric value={row.er_carry_annual} />)</span></Field>
      <Field label="Net cash / MC"><FractionMetric value={row.net_cash_to_market_cap} /></Field>
      <Field label="FCF yield"><FractionMetric value={row.fcf_yield} /></Field>
      <Field label="OCF yield"><FractionMetric value={row.ocf_yield} /></Field>
      <Field label="売上 / 営業益 YoY"><span className="font-mono tabular-nums"><FractionMetric value={row.sales_yoy} /> / <FractionMetric value={row.operating_profit_yoy} /></span></Field>
      <Field label="信用売残 / ADV"><span className="font-mono tabular-nums">{row.margin_short_to_adv === null || row.margin_short_to_adv === undefined ? '—' : `${row.margin_short_to_adv.toLocaleString('ja-JP', { maximumFractionDigits: 2 })} 日分`}</span></Field>
      <Field label="信用残の観測週"><span className="font-mono tabular-nums">{row.margin_week_end ?? '—'}</span></Field>
      <Field label="20d"><FractionMetric value={row.price_change_20d} /></Field>
      <Field label="52w low gap"><FractionMetric value={row.gap_from_52w_low} /></Field>
      <Field label="sector RS%"><FractionMetric value={row.sector_relative_strength_percentile} /></Field>
      <Field label="次決算"><span className="font-mono tabular-nums">{row.next_earnings_date ?? LABEL.earningsTbd}</span></Field>
      <Field label="データ品質"><span className="text-sm">{row.data_quality_flags.length === 0 ? 'なし' : row.data_quality_flags.join(' / ')}</span></Field>
    </dl>
  )
}

export function SecurityDetailPage() {
  const { ticker = '' } = useParams()
  const [data, setData] = useState<SecurityDetailView | null>(null)
  const [notFound, setNotFound] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setData(null)
    setNotFound(false)
    setError(null)
    fetchJson<SecurityDetailView>(`/api/securities/${encodeURIComponent(ticker)}`).then(setData).catch((reason: unknown) => {
      if (reason instanceof ApiError && reason.status === 404) setNotFound(true)
      else setError(reason instanceof Error ? reason.message : '銘柄情報を読み込めませんでした')
    })
  }, [ticker])

  if (notFound) return <PageState back message="この銘柄の記録はありません" mono title={`404 / ${ticker}`} />
  if (error) return <PageState message={error} mono title={ticker} />
  if (!data) return <LoadingPage label={`${ticker} の銘柄情報を読み込んでいます`} />

  const thesis = data.latest_thesis
  const averageCostYen = data.holding && data.holding.quantity !== 0
    ? data.holding.deployed_cost_yen / data.holding.quantity
    : null

  return (
    <PageShell
      above={(
        <nav className="flex items-center gap-2 text-sm text-muted-foreground" aria-label="パンくず">
          <Link className="underline-offset-4 hover:text-foreground hover:underline" to="/stocks">Stocks</Link>
          <span>/</span>
          <span className="font-mono text-foreground">{data.ticker}</span>
        </nav>
      )}
      // The heading is the ticker, so the name is what a reader actually recognises the
      // page by and keeps its weight; the sector trails it as context.
      lead={<><span className="font-medium text-foreground">{data.company_name ?? '名称なし'}</span> · {data.sector ?? 'sector —'}</>}
      meta={(
        <div className="flex flex-wrap items-center gap-2">
          {data.holding && <Badge variant="secondary">保有</Badge>}
          {data.revisions.length > 0 && <Badge variant="outline">research {data.revisions.length}</Badge>}
          <TradingViewButton labeled ticker={data.ticker} />
        </div>
      )}
      title={<span className="font-mono">{data.ticker}</span>}
      width="reading"
    >
      {data.holding && (
        <SectionCard description="portfolio ledger" meta={<AsOfBadge value={data.holding.market_price_as_of} />} padded title="現在の保有">
          <>
            <dl className="grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
              <Field label="数量"><span className="font-mono tabular-nums">{data.holding.quantity.toLocaleString('ja-JP')} 株</span></Field>
              <Field label="取得 / 現在">
                <span className="font-mono tabular-nums text-muted-foreground">取得 <YenAmount value={averageCostYen} /></span>
                <span className="ml-3 font-mono tabular-nums">現在 <YenAmount value={data.holding.market_price_yen === null ? null : Number(data.holding.market_price_yen)} /></span>
              </Field>
              <Field label="評価額"><YenAmount value={data.holding.market_value_yen} /></Field>
              <Field label="含み損益">
                <span>
                  <YenAmount sign tone="pnl" value={data.holding.unrealized_pnl_yen} /> <PctBadge tone="pnl" value={data.holding.unrealized_pnl_pct} />
                </span>
              </Field>
              <Field label="原評価のPmax（新規買付上限）"><YenAmount value={data.holding.pmax_raw_yen} /></Field>
              <Field label="Pmaxとの差"><PctBadge value={data.holding.pmax_gap_pct} /></Field>
              <Field label="判断"><Badge className="font-mono uppercase" variant="outline">{data.holding.disposition ?? '—'}</Badge></Field>
              <Field label="次決算"><span className="font-mono tabular-nums">{data.holding.next_earnings_date ?? LABEL.earningsTbd}</span></Field>
            </dl>
          </>
        </SectionCard>
      )}

      <SectionCard
        description="latest research"
        meta={thesis && <AsOfBadge value={thesis.revision.as_of} />}
        padded
        title="最新の企業評価"
      >
        <>
          {!thesis ? (
            <div className="py-8 text-center text-sm text-muted-foreground">research 記録なし</div>
          ) : (
            <div className="grid gap-6">
              <dl className="grid gap-4 sm:grid-cols-3">
                <Field label="企業評価"><span>{thesis.revision.disposition}</span></Field>
                <Field label="評価状態"><span>{thesis.revision.status}</span></Field>
                <Field label="Pmax（原評価・丸め前）"><YenAmount value={thesis.revision.pmax_raw_yen} /></Field>
              </dl>
              <p className="text-sm text-muted-foreground">candidate は配分検討に使える企業評価です。現在価格の条件、allocate の判断、実約定は別に確認します。</p>
              <ReviewedThesisContent projection={thesis.projection} />
              <code className="truncate rounded-md bg-muted px-3 py-2 text-xs text-muted-foreground" title={thesis.revision.thesis_id}>{thesis.revision.thesis_id}</code>
            </div>
          )}
        </>
      </SectionCard>

      <SectionCard description="過去の判断記録" meta={<Badge variant="secondary">{data.revisions.length} 件</Badge>} title="Research 履歴">
        {data.revisions.length === 0 ? (
          <p className="py-8 text-center text-sm text-muted-foreground">research 記録なし</p>
        ) : (
          <Table>
            <TableHeader className="bg-muted/60"><TableRow className="hover:bg-transparent"><TableHead>{LABEL.asOf}</TableHead><TableHead>判断</TableHead><TableHead className="text-right">Pmax（原評価）</TableHead><TableHead>状態</TableHead><TableHead>review</TableHead></TableRow></TableHeader>
            <TableBody>
              {data.revisions.map((revision) => (
                <TableRow key={revision.thesis_id}>
                  <TableCell className="font-mono tabular-nums">{revision.as_of}</TableCell>
                  <TableCell><Badge className="font-mono uppercase" variant="outline">{revision.disposition}</Badge></TableCell>
                  <TableCell className="text-right"><YenAmount value={revision.pmax_raw_yen} /></TableCell>
                  <TableCell>{revision.status}</TableCell>
                  <TableCell>{revision.review_id ? <Badge variant="secondary">有</Badge> : <span className="text-muted-foreground">—</span>}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </SectionCard>

      <SectionCard description="人間確認後に publish された保有判断" meta={<Badge variant="secondary">{data.position_reviews.length} 件</Badge>} title="Position Review 履歴">
        {data.position_reviews.length === 0 ? (
          <p className="py-8 text-center text-sm text-muted-foreground">Position Review 記録なし</p>
        ) : (
          <Table>
            <TableHeader className="bg-muted/60"><TableRow className="hover:bg-transparent"><TableHead>{LABEL.asOf}</TableHead><TableHead>action</TableHead><TableHead>thesis</TableHead><TableHead>note</TableHead></TableRow></TableHeader>
            <TableBody>
              {data.position_reviews.map((review) => (
                <TableRow key={review.position_review_id}>
                  <TableCell className="font-mono tabular-nums">{review.as_of}</TableCell>
                  <TableCell><Badge className="font-mono uppercase" variant="outline">{review.action ?? "未確定"}</Badge></TableCell>
                  <TableCell><code className="text-xs">{review.thesis_id}</code></TableCell>
                  <TableCell className="max-w-md text-sm text-muted-foreground">{review.note ?? '—'}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </SectionCard>

      {data.security_analysis && (
        <SectionCard
          description="latest screening"
          meta={data.screening_run && <AsOfBadge value={data.screening_run.as_of} />}
          padded
          title="Screening 指標"
        >
          <ScreeningMetrics row={data.security_analysis} />
        </SectionCard>
      )}
    </PageShell>
  )
}

function ReviewedThesisContent({ projection }: { projection: Record<string, unknown> }) {
  const object = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
  const text = (value: unknown) => value == null ? '未確認' : String(value)
  if (projection.status === 'requires_reassessment') return <div><p>旧形式の記録です。現在の判断には再評価が必要です。</p><pre className="overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify(projection.raw, null, 2)}</pre></div>
  const investment = object(projection.investment_case)
  const projections = object(projection.projections)
  return <div className="grid gap-4">
    <Field label="企業価値の変化と実現経路"><p>{text(investment.explanation)}</p></Field>
    <Field label="投資理由の成立性"><p>{text(investment.status)} — {text(investment.status_reason)}</p></Field>
    <Field label="重大な不成立条件"><p>{Array.isArray(investment.invalidation_conditions) ? investment.invalidation_conditions.join(' / ') : '未確認'}</p></Field>
    <Field label="最も強い反対仮説"><p>{text(projection.strongest_countercase)}</p></Field>
    {projection.status === 'unresolved' && <Field label="評価未解決の理由"><p>{text(projection.unresolved_reason)}</p></Field>}
    <p>評価起点 {text(projection.as_of)} / 観測価格 {text(projection.original_price_yen)} 円 / 日時 {text(projection.original_quote_at)} / basis {text(projection.original_price_basis)}</p>
    <p>期間 {text(projection.horizon_months)} か月 / 要求年率 {text(projection.required_annual_return_pct)}%</p>
    <div className="grid gap-4 sm:grid-cols-2">{['base', 'downside'].map(name => {
      const item = object(projections[name])
      return <div className="rounded border p-4" key={name}><strong>{name === 'base' ? 'Base' : 'Downside'}</strong>
        <p>分配後の価値 {text(item.terminal_value_per_share_yen)} 円 / 期間内分配 {text(item.cash_distribution_per_share_yen)} 円</p>
        <p>条件付き総 return {text(item.total_return_pct)}% / 年率換算 {text(item.annualized_return_pct)}%</p>
        <p className="mt-2 text-sm">{text(item.calculation)}</p></div>
    })}</div>
    <p className="text-sm text-muted-foreground">{text(projection.return_basis)}</p>
  </div>
}
