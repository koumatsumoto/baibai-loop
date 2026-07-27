import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { ApiError, fetchJson } from '../api/client'
import type { CandidateRowView, SecurityDetailView } from '../api/types'
import { AppShell } from '../components/AppShell'
import { AsOfBadge } from '../components/AsOfBadge'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageState } from '../components/PageState'
import { PctBadge } from '../components/PctBadge'
import { TradingViewButton } from '../components/TradingViewButton'
import { YenAmount } from '../components/YenAmount'
import { Badge } from '../components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/card'
import { Separator } from '../components/ui/separator'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../components/ui/table'
import { LABEL } from '../lib/labels'
import { pnlTone } from '../lib/portfolio'
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

function ScreeningMetrics({ row }: { row: CandidateRowView }) {
  return (
    <dl className="grid gap-6 sm:grid-cols-2 lg:grid-cols-5">
      <Field label="時価総額"><span className="font-mono tabular-nums">{row.market_cap_oku === null ? '—' : `${row.market_cap_oku.toLocaleString('ja-JP')} 億円`}</span></Field>
      <Field label="割安スコア"><span className="font-mono tabular-nums">{row.bargain_score ?? '—'}</span></Field>
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
    <>
      <AppShell />
      <main className="mx-auto grid max-w-[1400px] gap-6 px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
        <nav className="flex items-center gap-2 text-sm text-muted-foreground" aria-label="パンくず">
          <Link className="underline-offset-4 hover:text-foreground hover:underline" to="/stocks">Stocks</Link>
          <span>/</span>
          <span className="font-mono text-foreground">{data.ticker}</span>
        </nav>

        <header className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="font-mono text-3xl font-semibold tracking-tight">{data.ticker}</h1>
              {data.holding && <Badge variant="secondary">保有</Badge>}
              {data.revisions.length > 0 && <Badge variant="outline">research {data.revisions.length}</Badge>}
            </div>
            <p className="mt-2 text-lg font-medium">{data.company_name ?? '名称なし'}</p>
            <p className="mt-1 text-sm text-muted-foreground">{data.sector ?? 'sector —'}</p>
          </div>
          <TradingViewButton labeled ticker={data.ticker} />
        </header>

        {data.holding && (
          <Card className="shadow-sm">
            <CardHeader className="flex flex-row items-start justify-between gap-4">
              <div><CardTitle>現在の保有</CardTitle><CardDescription className="mt-1">portfolio ledger</CardDescription></div>
              <AsOfBadge value={data.holding.market_price_as_of} />
            </CardHeader>
            <CardContent>
              <dl className="grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
                <Field label="数量"><span className="font-mono tabular-nums">{data.holding.quantity.toLocaleString('ja-JP')} 株</span></Field>
                <Field label="取得 / 現在">
                  <span className="font-mono tabular-nums text-muted-foreground">取得 <YenAmount value={averageCostYen} /></span>
                  <span className="ml-3 font-mono tabular-nums">現在 <YenAmount value={Number(data.holding.market_price_yen)} /></span>
                </Field>
                <Field label="評価額"><YenAmount value={data.holding.market_value_yen} /></Field>
                <Field label="含み損益">
                  <span className={pnlTone(data.holding.unrealized_pnl_yen)}>
                    <YenAmount sign value={data.holding.unrealized_pnl_yen} /> <PctBadge tone="pnl" value={data.holding.unrealized_pnl_pct} />
                  </span>
                </Field>
                <Field label="FV"><YenAmount value={data.holding.fair_value_yen} /></Field>
                <Field label="FV乖離"><PctBadge value={data.holding.fv_gap_pct} /></Field>
                <Field label="判断"><Badge className="font-mono uppercase" variant="outline">{data.holding.recommendation ?? '—'}</Badge></Field>
                <Field label="次決算"><span className="font-mono tabular-nums">{data.holding.next_earnings_date ?? LABEL.earningsTbd}</span></Field>
              </dl>
            </CardContent>
          </Card>
        )}

        <Card className="shadow-sm">
          <CardHeader className="flex flex-row items-start justify-between gap-4">
            <div><CardTitle>最新の 5 年評価</CardTitle><CardDescription className="mt-1">latest research</CardDescription></div>
            {thesis && <time className="font-mono text-sm tabular-nums text-muted-foreground" dateTime={thesis.revision.as_of}>{thesis.revision.as_of}</time>}
          </CardHeader>
          <CardContent>
            {!thesis ? (
              <div className="py-8 text-center text-sm text-muted-foreground">research 記録なし</div>
            ) : (
              <div className="grid gap-6">
                <div className="grid overflow-hidden rounded-lg border sm:grid-cols-3 sm:divide-x">
                  {[
                    ['RECOMMENDATION', thesis.revision.recommendation],
                    ['CONFIDENCE', thesis.revision.confidence ?? '—'],
                    ['FAIR VALUE', <YenAmount key="fv" value={thesis.revision.current_fair_value_yen} />],
                  ].map(([label, value]) => (
                    <div className="border-b p-4 last:border-b-0 sm:border-b-0" key={String(label)}>
                      <p className="text-[10px] font-semibold tracking-wider text-muted-foreground">{label}</p>
                      <strong className="mt-2 block font-mono text-lg">{value}</strong>
                    </div>
                  ))}
                </div>

                <dl className="grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
                  <Field label="entry price basis"><YenAmount value={thesis.entry_price_basis_yen} /></Field>
                  <Field label="required 5y base CAGR"><PctBadge value={thesis.required_5y_base_cagr_pct} /></Field>
                  <Field label="permanent loss risks"><span className="font-mono tabular-nums">{thesis.permanent_loss_risk_count} axes</span></Field>
                  <Field label="model"><span>{thesis.revision.model_version ?? '—'}</span></Field>
                </dl>

                <div className="flex flex-wrap gap-2">
                  {thesis.scenarios.map((scenario) => <Badge key={`${scenario.name}-${scenario.horizon_years}`} variant="secondary">{scenario.name} · {scenario.horizon_years}Y</Badge>)}
                </div>

                <Separator />

                <dl className="grid gap-6 lg:grid-cols-3">
                  <Field label="Permanent loss conclusion"><p className="font-normal leading-relaxed">{thesis.permanent_loss_conclusion ?? '—'}</p></Field>
                  <Field label="Strongest countercase"><p className="font-normal leading-relaxed">{thesis.strongest_countercase ?? '—'}</p></Field>
                  <Field label="Sizing action"><p className="font-normal leading-relaxed">{thesis.sizing_action ?? '—'}</p></Field>
                </dl>
                <code className="truncate rounded-md bg-muted px-3 py-2 text-xs text-muted-foreground" title={thesis.revision.thesis_id}>{thesis.revision.thesis_id}</code>
              </div>
            )}
          </CardContent>
        </Card>

        <Card className="gap-0 overflow-hidden py-0 shadow-sm">
          <CardHeader className="flex flex-row items-start justify-between gap-4 border-b py-5">
            <div><CardTitle>Research 履歴</CardTitle><CardDescription className="mt-1">過去の判断記録</CardDescription></div>
            <Badge variant="secondary">{data.revisions.length} revisions</Badge>
          </CardHeader>
          {data.revisions.length === 0 ? (
            <CardContent className="py-8 text-center text-sm text-muted-foreground">research 記録なし</CardContent>
          ) : (
            <Table>
              <TableHeader className="bg-muted/60"><TableRow className="hover:bg-transparent"><TableHead>{LABEL.asOf}</TableHead><TableHead>判断</TableHead><TableHead className="text-right">FV</TableHead><TableHead>model</TableHead><TableHead>review</TableHead></TableRow></TableHeader>
              <TableBody>
                {data.revisions.map((revision) => (
                  <TableRow key={revision.thesis_id}>
                    <TableCell className="font-mono tabular-nums">{revision.as_of}</TableCell>
                    <TableCell><Badge className="font-mono uppercase" variant="outline">{revision.recommendation}</Badge></TableCell>
                    <TableCell className="text-right"><YenAmount value={revision.current_fair_value_yen} /></TableCell>
                    <TableCell>{revision.model_version ?? '—'}</TableCell>
                    <TableCell>{revision.review_id ? <Badge variant="secondary">有</Badge> : <span className="text-muted-foreground">—</span>}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </Card>

        <Card className="gap-0 overflow-hidden py-0 shadow-sm">
          <CardHeader className="flex flex-row items-start justify-between gap-4 border-b py-5">
            <div><CardTitle>Holding review 履歴</CardTitle><CardDescription className="mt-1">人間確認後に publish された保有判断</CardDescription></div>
            <Badge variant="secondary">{data.holding_reviews.length} revisions</Badge>
          </CardHeader>
          {data.holding_reviews.length === 0 ? (
            <CardContent className="py-8 text-center text-sm text-muted-foreground">holding review 記録なし</CardContent>
          ) : (
            <Table>
              <TableHeader className="bg-muted/60"><TableRow className="hover:bg-transparent"><TableHead>{LABEL.asOf}</TableHead><TableHead>action</TableHead><TableHead>thesis</TableHead><TableHead>note</TableHead></TableRow></TableHeader>
              <TableBody>
                {data.holding_reviews.map((review) => (
                  <TableRow key={review.holding_review_id}>
                    <TableCell className="font-mono tabular-nums">{review.as_of}</TableCell>
                    <TableCell><Badge className="font-mono uppercase" variant="outline">{review.action}</Badge></TableCell>
                    <TableCell><code className="text-xs">{review.thesis_id}</code></TableCell>
                    <TableCell className="max-w-md text-sm text-muted-foreground">{review.note ?? '—'}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </Card>

        {data.candidate_row && (
          <Card className="shadow-sm">
            <CardHeader className="flex flex-row items-start justify-between gap-4">
              <div><CardTitle>Screening 指標</CardTitle><CardDescription className="mt-1">latest screening</CardDescription></div>
              {data.candidate_run && <span className="font-mono text-xs tabular-nums text-muted-foreground">{LABEL.asOf} {data.candidate_run.asof_date}</span>}
            </CardHeader>
            <CardContent><ScreeningMetrics row={data.candidate_row} /></CardContent>
          </Card>
        )}
      </main>
    </>
  )
}
