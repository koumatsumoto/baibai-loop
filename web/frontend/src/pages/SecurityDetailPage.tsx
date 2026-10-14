import { useEffect, useState } from 'react'
import { Link, useLocation, useParams } from 'react-router'

import { ApiError, fetchJson } from '../api/client'
import type { SecurityAnalysisRowView, SecurityDetailView } from '../api/types'
import { AsOfBadge } from '../components/AsOfBadge'
import { ReviewedAssessmentContent } from '../components/ReviewedAssessmentContent'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageShell } from '../components/PageShell'
import { ResearchNav } from '../components/ResearchNav'
import { SecurityMarketContextSection } from './research/MarketEvidence'
import { SectionCard } from '../components/SectionCard'
import { PageState } from '../components/PageState'
import { PctBadge } from '../components/PctBadge'
import { TradingViewButton } from '../components/TradingViewButton'
import { YenAmount } from '../components/YenAmount'
import { Badge } from '../components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../components/ui/table'
import { LABEL } from '../lib/labels'
import { ACTION_LABELS, STANCE_LABELS } from '../lib/research'
import { cn } from '../lib/utils'

import { ExternalAssessmentSection } from './stocks/ExternalAssessmentSection'

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

export function ScreeningMetrics({ row }: { row: SecurityAnalysisRowView }) {
  return (
    <dl className="grid gap-6 sm:grid-cols-2 lg:grid-cols-5">
      <Field label="時価総額"><span className="font-mono tabular-nums">{row.market_cap_oku === null ? '—' : `${row.market_cap_oku.toLocaleString('ja-JP')} 億円`}</span></Field>
      <Field label="機械・業種中央値 anchor / 乖離"><span className="font-mono tabular-nums">{row.fair_value_anchor_yen === null ? '—' : `${row.fair_value_anchor_yen.toLocaleString('ja-JP')} 円`} / <PctBadge value={row.fair_value_gap_pct} /></span></Field>
      <Field label="PER / forward"><span className="font-mono tabular-nums">{row.per_trailing ?? '—'} / {row.per_forward ?? '—'}</span></Field>
      <Field label="PBR"><span className="font-mono tabular-nums">{row.pbr ?? '—'}</span></Field>
      <Field label="EV/EBITDA"><span className="font-mono tabular-nums">{row.ev_ebitda ?? '—'}</span></Field>
      <Field label="P/S"><span className="font-mono tabular-nums">{row.p_s ?? '—'}</span></Field>
      <Field label="PCFR"><span className="font-mono tabular-nums">{row.pcfr ?? '—'}</span></Field>
      <Field label="配当利回り">{row.dividend_yield === null ? '未観測・未記録' : <FractionMetric value={row.dividend_yield} />}<span className="text-xs text-muted-foreground">basis: {row.dividend_basis ?? '未記録'}</span></Field>
      <Field label="発行済株式数YoY入力">{row.net_share_change_yoy == null ? '未観測・未記録' : <FractionMetric value={row.net_share_change_yoy} />}</Field>
      <Field label="機械E[r]（%/年 · reversion / carry）"><span className="font-mono tabular-nums"><FractionMetric value={row.er_annual} /> (<FractionMetric value={row.er_reversion_annual} /> / <FractionMetric value={row.er_carry_annual} />)</span></Field>
      <Field className="sm:col-span-2 lg:col-span-5" label="機械E[r]のモデル・前提">
        <p>モデル {row.er_model_version ?? '未確認'} · 保存単位 {row.er_unit ?? '未確認'} · anchor指標 {row.er_anchor_metrics ?? '未確認'}</p>
        <p className="whitespace-pre-wrap">{row.er_assumptions ?? '保存された前提はありません。'}</p>
        {row.carry_missing_assumption && <p className="text-xs text-muted-foreground">欠測の成分は計算上0寄与。無配・株数不変の確認ではありません。</p>}
      </Field>
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
  const origin = useLocation().state as { exploration?: string } | null
  const exploration = origin?.exploration ?? '/stocks/explore'
  const [data, setData] = useState<SecurityDetailView | null>(null)
  const [notFound, setNotFound] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    setData(null)
    setNotFound(false)
    setError(null)
    fetchJson<SecurityDetailView>(`/api/securities/${encodeURIComponent(ticker)}`, { signal: controller.signal })
      .then((result) => {
        if (!controller.signal.aborted) setData(result)
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted || (reason instanceof Error && reason.name === 'AbortError')) return
        if (reason instanceof ApiError && reason.status === 404) setNotFound(true)
        else setError(reason instanceof Error ? reason.message : '銘柄情報を読み込めませんでした')
      })
    return () => controller.abort()
  }, [ticker])

  if (notFound) return <PageState back message="この銘柄の記録はありません" mono title={`404 / ${ticker}`} />
  if (error) return <PageState message={error} mono title={ticker} />
  if (!data) return <LoadingPage label={`${ticker} の銘柄情報を読み込んでいます`} />

  const assessment = data.latest_assessment
  const averageCostYen = data.holding && data.holding.quantity !== 0
    ? data.holding.deployed_cost_yen / data.holding.quantity
    : null

  return (
    <PageShell
      above={(
        <div className="grid gap-4"><ResearchNav /><nav className="flex items-center gap-2 text-sm text-muted-foreground" aria-label="パンくず">
          <Link className="underline-offset-4 hover:text-foreground hover:underline" to={exploration}>候補探索</Link>
          <span>/</span>
          <span className="font-mono text-foreground">{data.ticker}</span>
        </nav></div>
      )}
      lead={<>{data.ticker} · {data.sector ?? '業種未確認'}</>}
      meta={(
        <div className="flex flex-wrap items-center gap-2">
          {data.holding && <Badge variant="secondary">保有</Badge>}
          {data.revisions.length > 0 && <Badge variant="outline">企業調査 {data.revisions.length}</Badge>}
          <TradingViewButton labeled ticker={data.ticker} />
        </div>
      )}
      title={data.company_name ?? data.ticker}
      width="reading"
    >
      <nav aria-label="銘柄の根拠" className="flex flex-wrap gap-x-4 gap-y-2 text-sm"><a className="underline" href="#research">企業評価</a>{data.security_analysis && <a className="underline" href="#valuation">価値の指標</a>}<a className="underline" href="#market-context">市場の指標</a><a className="underline" href="#external-assessment">外部参考評価</a></nav>
      <div className="scroll-mt-24" id="research">
      <SectionCard
        description="latest research"
        meta={assessment && <AsOfBadge value={assessment.revision.as_of} />}
        padded
        title="最新の企業評価"
      >
        <>
          {!assessment ? (
            <div className="py-8 text-center text-sm text-muted-foreground">research 記録なし</div>
          ) : (
            <div className="grid gap-6">
              <dl className="grid gap-4 sm:grid-cols-3">
                <Field label="企業評価"><span>{STANCE_LABELS[assessment.revision.stance] ?? assessment.revision.stance}</span></Field>
                <Field label="評価状態"><span>{assessment.revision.status}</span></Field>
                <Field label="Pmax（原評価・丸め前）"><YenAmount value={assessment.revision.pmax_original_yen} /></Field>
              </dl>
              <p className="text-sm text-muted-foreground">supported は評価の根拠が確認された結論です。購入提案・採否・実約定は別に確認します。</p>
              <ReviewedAssessmentContent document={assessment.document} projection={assessment.projection} />
              <Link className="w-fit text-sm underline" to={`/stocks/stock-assessments/${assessment.revision.assessment_id}`}>この評価版と独立 Review を見る →</Link>
            </div>
          )}
        </>
      </SectionCard>

      </div>
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
              <Field label="企業評価"><Badge variant="outline">{data.holding.stance ? STANCE_LABELS[data.holding.stance] ?? data.holding.stance : '—'}</Badge></Field>
              <Field label="次決算"><span className="font-mono tabular-nums">{data.holding.next_earnings_date ?? LABEL.earningsTbd}</span></Field>
            </dl>
          </>
        </SectionCard>
      )}

      <details className="rounded-lg border bg-card p-4"><summary className="cursor-pointer font-medium">保存した評価・提案の履歴</summary><div className="mt-4 grid gap-4"><SectionCard description="根拠と条件を固定した評価版" meta={<Badge variant="secondary">{data.revisions.length} 件</Badge>} title="銘柄評価の履歴">
        {data.revisions.length === 0 ? (
          <p className="py-8 text-center text-sm text-muted-foreground">research 記録なし</p>
        ) : (
          <Table>
            <TableHeader className="bg-muted/60"><TableRow className="hover:bg-transparent"><TableHead>{LABEL.asOf}</TableHead><TableHead>判断</TableHead><TableHead className="text-right">Pmax（原評価）</TableHead><TableHead>状態</TableHead><TableHead>review</TableHead></TableRow></TableHeader>
            <TableBody>
              {data.revisions.map((revision) => (
                <TableRow key={revision.assessment_id}>
                  <TableCell className="font-mono tabular-nums">{revision.as_of}</TableCell>
                  <TableCell><Link className="underline" to={`/stocks/stock-assessments/${revision.assessment_id}`}>{STANCE_LABELS[revision.stance] ?? revision.stance}</Link></TableCell>
                  <TableCell className="text-right"><YenAmount value={revision.pmax_original_yen} /></TableCell>
                  <TableCell>{revision.status}</TableCell>
                  <TableCell>{revision.review_id ? <Badge variant="secondary">有</Badge> : <span className="text-muted-foreground">—</span>}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </SectionCard>

      <SectionCard description="購入・保有・売却・見送り・保留の保存提案。採否・注文・約定は別工程です。" meta={<Badge variant="secondary">{data.investment_proposals.length} 件</Badge>} title="投資提案の履歴">
        {data.investment_proposals.length === 0 ? (
          <p className="py-8 text-center text-sm text-muted-foreground">投資提案の記録なし</p>
        ) : (
          <Table>
            <TableHeader className="bg-muted/60"><TableRow className="hover:bg-transparent"><TableHead>{LABEL.asOf}</TableHead><TableHead>行動</TableHead><TableHead>提案</TableHead></TableRow></TableHeader>
            <TableBody>
              {data.investment_proposals.map((proposal) => (
                <TableRow key={proposal.proposal_id}>
                  <TableCell className="font-mono tabular-nums">{proposal.as_of}</TableCell>
                  <TableCell>{proposal.actions.map(action => ACTION_LABELS[action] ?? action).join(' / ')}</TableCell>
                  <TableCell><Link className="text-sm underline" to={`/stocks/investment-proposals/${proposal.proposal_id}`}>{proposal.headline}</Link></TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </SectionCard>

      </div></details>

      {data.security_analysis && <div className="scroll-mt-24" id="valuation"><SectionCard description="保存済みの機械分析。企業固有の評価や投資提案とは別です。" meta={data.screening_run && <AsOfBadge value={data.screening_run.as_of} />} padded title="価値の指標">
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3">
          <Field label="PER 予想 / 実績">{data.security_analysis.per_forward ?? '未観測'} / {data.security_analysis.per_trailing ?? '未観測'}</Field>
          <Field label="PBR">{data.security_analysis.pbr ?? '未観測'}</Field>
          <Field label="機械E[r]（%/年）"><FractionMetric value={data.security_analysis.er_annual} /></Field>
          <Field label="配当利回り"><FractionMetric value={data.security_analysis.dividend_yield} /></Field>
          <Field label="FCF利回り"><FractionMetric value={data.security_analysis.fcf_yield} /></Field>
          <Field label="Net cash / 時価総額"><FractionMetric value={data.security_analysis.net_cash_to_market_cap} /></Field>
        </dl>
        {data.security_analysis.carry_missing_assumption && <p className="mt-4 text-xs text-muted-foreground">E[r]の欠測成分は計算上0寄与です。無配・株数不変の確認ではありません。</p>}
        <details className="mt-4"><summary className="cursor-pointer text-sm">全指標・計算前提・入力の注意</summary><div className="mt-4"><ScreeningMetrics row={data.security_analysis} /></div></details>
      </SectionCard></div>}
      <div className="scroll-mt-24" id="market-context"><SecurityMarketContextSection context={data.market_context} /></div>
      <div className="scroll-mt-24" id="external-assessment"><ExternalAssessmentSection assessment={data.external_assessment} /></div>
      <Link className="text-sm underline" to="/stocks">調査・提案を見る →</Link>
    </PageShell>
  )
}
