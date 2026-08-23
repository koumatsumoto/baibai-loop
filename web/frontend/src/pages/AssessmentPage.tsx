import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router'
import { ArrowLeft, CircleAlert } from 'lucide-react'

import { fetchJson } from '../api/client'
import type { AssessmentCaseView, AssessmentPurchaseView, BargainAssessmentView } from '../api/types'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageShell } from '../components/PageShell'
import { SectionCard } from '../components/SectionCard'
import { PageState } from '../components/PageState'
import { PctBadge } from '../components/PctBadge'
import { TradingViewButton } from '../components/TradingViewButton'
import { Alert, AlertDescription, AlertTitle } from '../components/ui/alert'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../components/ui/table'
import { ASSESSMENT_RESULT, ASSESSMENT_TONE_CLASS as TONE_CLASS, CASE_DISPOSITION, headroomToMaxPct, limitVsClosePct, orderCases, PERMANENT_LOSS_LABEL, PERMANENT_LOSS_TONE, purchaseAlerts } from '../lib/assessment'
import { EMPTY, formatJstDateTime, formatNumber, formatYen } from '../lib/format'
import { LABEL } from '../lib/labels'
import { cn } from '../lib/utils'

// The research digest, in the order the argument is built: what the business is, whether
// it keeps what it earns, whether that lasts, whether it survives being wrong, what would
// break the case, and what pays the reader back.
const CASE_SECTIONS: readonly (readonly [keyof AssessmentCaseView, string])[] = [
  ['business_model', '事業モデル'],
  ['value_capture', '価値の取り分'],
  ['growth_quality', '成長の質'],
  ['financial_resilience', '財務耐性'],
  ['strongest_countercase', '最も強い反対仮説'],
  ['catalyst', 'catalyst'],
]

// Per-case machine values, all derived by the engine from the thesis and verified against
// it at publish time. The buffers are what decide the case, so they lead.
const MACHINE_ROWS: readonly (readonly [keyof AssessmentCaseView, string, 'pct' | 'pp' | 'yen' | 'x'])[] = [
  ['five_year_base_cagr_pct', '5年 base CAGR', 'pct'],
  ['required_return_pct', '要求リターン', 'pct'],
  ['fair_value_yen', 'FV', 'yen'],
  ['fv_gap_pct', 'FV 乖離', 'pct'],
  ['observed_trailing_multiple', '実績倍率', 'x'],
  ['base_terminal_multiple', 'base 終端倍率', 'x'],
  ['break_even_terminal_multiple', '損益分岐 終端倍率', 'x'],
  ['terminal_multiple_buffer', '終端倍率バッファ', 'x'],
  ['break_even_earnings_growth_pct', '損益分岐 利益成長', 'pct'],
  ['earnings_growth_buffer_pp', '利益成長バッファ', 'pp'],
]

function machineCell(assessmentCase: AssessmentCaseView, key: keyof AssessmentCaseView, unit: 'pct' | 'pp' | 'yen' | 'x') {
  const value = assessmentCase[key]
  if (typeof value !== 'number') return <span className="text-muted-foreground">{EMPTY}</span>
  if (unit === 'pct') return <PctBadge value={value} />
  if (unit === 'pp') return <span className={cn('font-mono font-medium tabular-nums', value > 0 ? 'text-positive' : value < 0 && 'text-destructive')}>{value > 0 ? '+' : ''}{formatNumber(value, 1)} pp</span>
  if (unit === 'yen') return <span className="font-mono tabular-nums">{formatNumber(value, 0)} 円</span>
  return <span className={cn('font-mono tabular-nums', key === 'terminal_multiple_buffer' && (value > 0 ? 'font-medium text-positive' : value < 0 && 'font-medium text-destructive'))}>{value > 0 && key === 'terminal_multiple_buffer' ? '+' : ''}{formatNumber(value, 2)}x</span>
}

function DispositionBadge({ disposition }: { disposition: string }) {
  const meta = CASE_DISPOSITION[disposition] ?? { label: disposition, tone: 'muted' as const }
  return <Badge className={cn('font-semibold', TONE_CLASS[meta.tone])}>{meta.label}</Badge>
}

function PurchaseCard({ purchase }: { purchase: AssessmentPurchaseView }) {
  const alerts = purchaseAlerts(purchase, new Date())
  const limitGap = limitVsClosePct(purchase)
  const headroom = headroomToMaxPct(purchase)
  return (
    <SectionCard
      description={<>指値 {formatYen(purchase.limit_price_yen)} × {purchase.quantity.toLocaleString('ja-JP')} 株 · {LABEL.published}時点 proposal <span className="font-mono">{purchase.proposal_id}</span></>}
      meta={(
        <div className="flex flex-wrap items-center gap-3">
          <Link className="font-mono text-sm font-semibold underline-offset-4 hover:underline" to={`/securities/${purchase.ticker}`}>{purchase.ticker}</Link>
          <TradingViewButton ticker={purchase.ticker} />
        </div>
      )}
      padded
      title="購入方法"
    >
      <div className="grid gap-4">
        <dl className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3 lg:grid-cols-6">
          {[
            ['指値', formatYen(purchase.limit_price_yen)],
            ['数量', `${purchase.quantity.toLocaleString('ja-JP')} 株`],
            ['約定金額', formatYen(purchase.notional_yen)],
            ['上限価格', formatYen(purchase.max_acceptable_price_yen)],
            [`終値（${purchase.price_as_of}）`, formatYen(purchase.close_yen)],
            ['発注期限', formatJstDateTime(purchase.expires_at)],
          ].map(([label, value]) => (
            <div key={label}>
              <dt className="text-[10px] font-semibold tracking-wide text-muted-foreground">{label}</dt>
              <dd className="mt-1 font-mono text-sm font-medium tabular-nums">{value}</dd>
            </div>
          ))}
        </dl>
        <div className="flex flex-wrap gap-x-6 gap-y-1 border-t pt-3 text-xs text-muted-foreground">
          <span>終値に対する指値: {limitGap === null ? EMPTY : <PctBadge value={limitGap} />}</span>
          <span>上限までの余地: {headroom === null ? EMPTY : <PctBadge value={headroom} />}</span>
          <span>proposal の現在状態: <strong className="text-foreground">{purchase.current_status ?? '見つかりません'}</strong></span>
        </div>
        {purchase.warnings.length > 0 && (
          <div className="grid gap-1.5">
            {purchase.warnings.map((warning) => (
              <p className="text-sm text-warning" key={warning}>publish 時点の warning: {warning}</p>
            ))}
          </div>
        )}
        {alerts.map((alert) => (
          <Alert key={alert.message} role="note" variant={alert.severity === 'warning' ? 'destructive' : 'default'}>
            <CircleAlert />
            <AlertTitle>{alert.severity === 'warning' ? '発注前に確認' : '公表後の変化'}</AlertTitle>
            <AlertDescription>{alert.message}</AlertDescription>
          </Alert>
        ))}
      </div>
    </SectionCard>
  )
}

function CaseComparison({ cases }: { cases: readonly AssessmentCaseView[] }) {
  return (
    <SectionCard description="個別リサーチの thesis から engine が導いた値。publish 時に thesis と突合済みです。" padded title="case 横比較">
        <div className="overflow-x-auto">
          <Table className="text-sm">
            <TableHeader>
              <TableRow>
                <TableHead className="min-w-44">指標</TableHead>
                {cases.map((assessmentCase) => (
                  <TableHead className="min-w-32 text-right" key={assessmentCase.ticker}>
                    <Link className="font-mono font-semibold underline-offset-4 hover:underline" to={`/securities/${assessmentCase.ticker}`}>{assessmentCase.ticker}</Link>
                    <span className="ml-1.5 font-normal text-muted-foreground">{assessmentCase.name ?? ''}</span>
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              <TableRow>
                <TableCell className="font-medium">判定</TableCell>
                {cases.map((assessmentCase) => <TableCell className="text-right" key={assessmentCase.ticker}><DispositionBadge disposition={assessmentCase.disposition} /></TableCell>)}
              </TableRow>
              <TableRow>
                <TableCell className="font-medium">永久損失</TableCell>
                {cases.map((assessmentCase) => (
                  <TableCell className="text-right" key={assessmentCase.ticker}>
                    {assessmentCase.permanent_loss_conclusion === null
                      ? <span className="text-muted-foreground">{EMPTY}</span>
                      : <Badge className={cn('font-semibold', TONE_CLASS[PERMANENT_LOSS_TONE[assessmentCase.permanent_loss_conclusion] ?? 'muted'])}>{PERMANENT_LOSS_LABEL[assessmentCase.permanent_loss_conclusion] ?? assessmentCase.permanent_loss_conclusion}</Badge>}
                    {assessmentCase.adverse_risk_axes.length > 0 && (
                      <p className="mt-1 text-[10px] text-warning">不利: {assessmentCase.adverse_risk_axes.join(', ')}</p>
                    )}
                  </TableCell>
                ))}
              </TableRow>
              {MACHINE_ROWS.map(([key, label, unit]) => (
                <TableRow key={key}>
                  <TableCell className="font-medium">{label}</TableCell>
                  {cases.map((assessmentCase) => <TableCell className="text-right" key={assessmentCase.ticker}>{machineCell(assessmentCase, key, unit)}</TableCell>)}
                </TableRow>
              ))}
              <TableRow>
                <TableCell className="font-medium">判定理由</TableCell>
                {/* The only prose in the table. Cells do not wrap by default, so without
                    this one sentence stretches the table past the viewport and pushes
                    every value in every row out of sight. */}
                {cases.map((assessmentCase) => <TableCell className="max-w-96 min-w-64 text-left align-top whitespace-normal text-muted-foreground" key={assessmentCase.ticker}>{assessmentCase.disposition_reason}</TableCell>)}
              </TableRow>
            </TableBody>
          </Table>
        </div>
    </SectionCard>
  )
}

function CaseCard({ assessmentCase }: { assessmentCase: AssessmentCaseView }) {
  return (
    <Card className="gap-0 overflow-hidden py-0 shadow-sm">
      <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-3 border-b bg-muted/40 px-5 py-4">
        <div>
          <CardTitle className="text-base">
            <Link className="font-mono underline-offset-4 hover:underline" to={`/securities/${assessmentCase.ticker}`}>{assessmentCase.ticker}</Link>
            <span className="ml-2 font-normal">{assessmentCase.name ?? ''}</span>
          </CardTitle>
          <CardDescription className="font-mono text-xs">
            thesis {assessmentCase.thesis_id}{assessmentCase.review_id !== null && ` · review ${assessmentCase.review_id}`}
          </CardDescription>
        </div>
        <div className="flex items-center gap-2">
          <DispositionBadge disposition={assessmentCase.disposition} />
          <TradingViewButton ticker={assessmentCase.ticker} />
        </div>
      </CardHeader>
      <CardContent className="grid gap-3 px-5 py-4">
        <div className="rounded-md border bg-muted/30 p-3">
          <span className="text-xs font-semibold text-muted-foreground">判定理由</span>
          <p className="text-sm font-medium">{assessmentCase.disposition_reason}</p>
        </div>
        {CASE_SECTIONS.map(([key, heading]) => {
          const text = assessmentCase[key]
          if (typeof text !== 'string' || text === '') return null
          return (
            <div key={key}>
              <h4 className="text-sm font-semibold text-accent-foreground/90">{heading}</h4>
              <p className="text-sm text-muted-foreground">{text}</p>
            </div>
          )
        })}
        {assessmentCase.research_questions.length > 0 && (
          <div>
            {/* Why this candidate earned a research slot, and whether the research settled
                it. An unresolved question is shown, not dropped. */}
            <h4 className="text-sm font-semibold text-accent-foreground/90">選定時の確認事項</h4>
            <div className="grid gap-2">
              {assessmentCase.research_questions.map((item) => (
                <div className="rounded-md border p-2.5" key={item.question}>
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge className={cn('text-[10px] font-semibold', item.status === 'answered' ? TONE_CLASS.positive : TONE_CLASS.warning)}>
                      {item.status === 'answered' ? '決着' : '未決着'}
                    </Badge>
                    <span className="text-sm font-medium">{item.question}</span>
                  </div>
                  <p className="mt-1 text-sm text-muted-foreground">{item.answer}</p>
                </div>
              ))}
            </div>
          </div>
        )}
        {assessmentCase.unknowns.length > 0 && (
          <div>
            <h4 className="text-sm font-semibold text-accent-foreground/90">未解決の不確実性</h4>
            <ul className="ml-4 list-disc text-sm text-muted-foreground">
              {assessmentCase.unknowns.map((item) => <li key={item}>{item}</li>)}
            </ul>
          </div>
        )}
        {assessmentCase.source_caveats.length > 0 && (
          <div>
            <h4 className="text-sm font-semibold text-warning">出典の欠落と判断への影響</h4>
            <div className="grid gap-1.5">
              {assessmentCase.source_caveats.map((caveat) => (
                <p className="text-sm text-muted-foreground" key={`${caveat.source_id}-${caveat.status}`}>
                  <Badge className="mr-1.5 text-[10px]" variant="outline">{caveat.status}</Badge>
                  <span className="font-mono text-xs">{caveat.source_id}</span> — {caveat.decision_impact}
                </p>
              ))}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

export function AssessmentPage() {
  const { assessmentId = '' } = useParams<{ assessmentId: string }>()
  const [data, setData] = useState<BargainAssessmentView | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setError(null)
    setData(null)
    fetchJson<BargainAssessmentView>(`/api/assessments/${encodeURIComponent(assessmentId)}`)
      .then(setData)
      .catch((reason: unknown) => {
        setError(reason instanceof Error ? reason.message : '提案レポートを読み込めませんでした')
      })
  }, [assessmentId])

  const cases = useMemo(() => orderCases(data?.cases ?? []), [data])

  if (error) return <PageState message={error} title="提案レポート" />
  if (!data) return <LoadingPage label="提案レポートを読み込んでいます" />

  const result = ASSESSMENT_RESULT[data.result] ?? { label: data.result, tone: 'muted' as const }

  return (
    // The page is named for what it is; the headline is data and stays in the card, where
    // a long sentence reads as a sentence rather than as a heading.
    <PageShell
      above={<div><Button asChild size="sm" variant="ghost"><Link to="/stocks"><ArrowLeft />Stocks に戻る</Link></Button></div>}
      meta={<Badge className={cn('font-semibold', TONE_CLASS[result.tone])}>{result.label}</Badge>}
      title="割安機会評価"
      width="reading"
    >
      <Card className="shadow-sm">
        <CardHeader className="gap-3">
          <CardTitle className="text-xl">{data.headline}</CardTitle>
          <CardDescription className="flex flex-wrap gap-x-4 gap-y-1">
            <span className="font-mono">{data.assessment_id}</span>
            <span>{LABEL.asOf} {data.as_of}</span>
            <span>{LABEL.published} {formatJstDateTime(data.published_at)}</span>
            <Link className="underline-offset-4 hover:text-foreground hover:underline" to="/stocks/shortlist">選定レポート {data.shortlist_id} →</Link>
            {data.macro_context_id !== null && (
              <Link className="underline-offset-4 hover:text-foreground hover:underline" to={`/macro/reports/${data.macro_context_id}`}>macro context {data.macro_context_id} →</Link>
            )}
          </CardDescription>
        </CardHeader>
      </Card>

      {data.purchase !== null && <PurchaseCard purchase={data.purchase} />}

      {data.entry_timing !== null && (
        <SectionCard description="いま買う理由と、待つ場合に何を待つのか" padded title="entry timing">
          <p className="text-sm">{data.entry_timing}</p>
        </SectionCard>
      )}

      <SectionCard description="case 間の比較で何が決め手になったか" padded title="なぜこの結論か">
        <p className="text-sm whitespace-pre-line">{data.comparison}</p>
      </SectionCard>

      {cases.length > 0 && <CaseComparison cases={cases} />}

      <div className="grid gap-4">
        {cases.map((assessmentCase) => <CaseCard key={assessmentCase.ticker} assessmentCase={assessmentCase} />)}
      </div>

      <SectionCard description="この判断で諦めた機会と、その代償の見立て" padded title="見送ったもの">
        <p className="text-sm whitespace-pre-line">{data.forgone}</p>
      </SectionCard>

      <SectionCard padded title="内容レビュー">
        <div className="grid gap-2">
          <p className="text-sm">
            <span className="font-mono">{data.review.reviewer_identity}</span> · attempt {data.review.attempt} · {formatJstDateTime(data.review.reviewed_at)} · <strong>{data.review.conclusion}</strong>
          </p>
          {data.review.open_findings.length > 0 && (
            <div>
              <h4 className="text-sm font-semibold text-warning">未解消の指摘</h4>
              <ul className="ml-4 list-disc text-sm text-muted-foreground">
                {data.review.open_findings.map((finding) => <li key={finding}>{finding}</li>)}
              </ul>
            </div>
          )}
        </div>
      </SectionCard>

      <p className="text-xs text-muted-foreground">
        この文書は publish 時点で確定した判断で、以後書き換わりません。指値・数量は proposal に紐づく発注計画で、現在の proposal 状態は上のカードに表示しています。
      </p>
    </PageShell>
  )
}
