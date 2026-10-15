import { useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router'

import { fetchJson } from '../api/client'
import type { InvestmentProposalItemView, InvestmentProposalPreviewView, InvestmentProposalView } from '../api/types'
import { AsOfBadge } from '../components/AsOfBadge'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageShell } from '../components/PageShell'
import { PageState } from '../components/PageState'
import { ResearchNav } from '../components/ResearchNav'
import { ReviewedAssessmentContent } from '../components/ReviewedAssessmentContent'
import { SectionCard } from '../components/SectionCard'
import { UpdatedAtBadge } from '../components/UpdatedAtBadge'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { formatJstStamp, formatNumber } from '../lib/format'
import { ACTION_LABELS, RETURN_UNAVAILABLE_LABELS, proposalWarningLabel, researchRecord as record, researchText as text } from '../lib/research'

function yen(value: string | number | null | undefined): string {
  return value == null ? '未確認' : `${formatNumber(Number(value), 2)}円`
}

function SavedTermsExpiry({ value }: { value: string }) {
  const [viewedAt] = useState(() => Date.now())
  return <>{formatJstStamp(value)}{new Date(value).getTime() <= viewedAt && <span className="ml-2 text-muted-foreground">表示時点で期限経過</span>}</>
}

export function ProposalItemContent({ item, savedQuote }: { item: InvestmentProposalItemView; savedQuote?: Record<string, unknown> }) {
  const terms = item.terms
  const quotePrice = savedQuote?.price_yen
  const quoted = typeof quotePrice === 'string' || typeof quotePrice === 'number'
  return <article className="grid scroll-mt-24 gap-3 rounded-lg border p-4" id={`proposal-item-${item.ticker}`}>
    <div className="flex flex-wrap items-center justify-between gap-2"><Link className="font-mono font-semibold underline" to={`/securities/${item.ticker}`}>{item.ticker}</Link><Badge variant={item.action === 'buy' ? 'default' : 'secondary'}>{ACTION_LABELS[item.action] ?? item.action}</Badge></div>
    <p className="whitespace-pre-line text-sm">{item.rationale}</p>
    {quoted && <p className="text-sm">提案時の実 quote {yen(quotePrice)} · {text(savedQuote?.price_as_of)}{terms && <span> · 実 quote と提案指値の差 {yen(Number(quotePrice) - Number(terms.limit_price_yen))}</span>}</p>}
    {item.action === 'hold' && <div className="text-sm"><p>保有の根拠: {item.holding_basis === 'investment_case_intact' ? '投資仮説の維持を確認' : '現在からの見返りを確認'}</p><p>{item.case_reassessment_reason}</p>{item.return_calculation_unavailable_reason && <p>現在収益の再計算ができない理由: {RETURN_UNAVAILABLE_LABELS[item.return_calculation_unavailable_reason] ?? item.return_calculation_unavailable_reason}</p>}</div>}
    {item.action === 'sell' && <div className="text-sm"><p>売却理由: {item.reason_code === 'economic_invalidation' ? '投資仮説の破綻' : '残存見返りが不十分'}</p><p>{item.case_reassessment_reason}</p></div>}
    {item.remaining_reward && <p className="text-sm">残存見返り: {text(item.remaining_reward.status)} · {text(item.remaining_reward.reason)}</p>}
    {(item.action === 'buy' || item.action === 'sell') && <div className="grid gap-3 rounded border p-4">
      <p className="font-medium">保存した提案条件 · {item.order_readiness === 'ready' ? '提案条件確定' : '価格・数量等の条件未確定'}</p>
      {terms ? <><dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-3">
        <div><dt className="text-xs text-muted-foreground">指値</dt><dd>{yen(terms.limit_price_yen)}</dd></div>
        <div><dt className="text-xs text-muted-foreground">数量</dt><dd>{formatNumber(terms.quantity)}株</dd></div>
        <div><dt className="text-xs text-muted-foreground">金額</dt><dd>{yen(terms.notional_yen)}</dd></div>
        <div><dt className="text-xs text-muted-foreground">対象 session</dt><dd>{terms.target_session}</dd></div>
        <div><dt className="text-xs text-muted-foreground">期限</dt><dd><SavedTermsExpiry value={terms.expires_at} /></dd></div>
        {terms.side === 'buy' && <div><dt className="text-xs text-muted-foreground">評価の購入上限</dt><dd>{yen(terms.maximum_entry_price_yen)}</dd></div>}
      </dl>{item.timing && <p className="text-sm">{item.timing === 'wait_limit' ? '指定の指値で待つ提案' : '当時の現在価格を上限とする指値提案'}</p>}
      <p className="text-xs text-muted-foreground">注文受理・約定の保証ではありません。注文前に現在の条件を確認してください。</p>
      <ul className="list-inside list-disc text-sm">{terms.conditions.map(condition => <li key={condition}>{condition}</li>)}</ul>
      {terms.warnings.length > 0 && <p className="break-words text-xs text-muted-foreground">当時の条件に対する警告: {terms.warnings.map(proposalWarningLabel).join(' / ')}</p>}
      </> : <>{item.proposed_limit_yen !== null && <p className="text-sm">希望価格の上限 {yen(item.proposed_limit_yen)} · 具体的な提案条件は未確定です。</p>}<ul className="list-inside list-disc text-sm">{item.terms_unavailable_reasons.map(reason => <li key={reason.code}>{reason.message}</li>)}</ul></>}
    </div>}
    {item.action === 'defer' && <div className="grid gap-2 text-sm"><p className="font-medium">判断を保留する不足</p><ul className="list-inside list-disc">{item.blockers.map(reason => <li key={reason.code}>{reason.message}</li>)}</ul><p>再開条件: {item.resume_conditions.join(' / ')}</p></div>}
    <p className="text-sm">見直し条件: {item.review_conditions.join(' / ')}</p>
    {item.assessment_ref ? <Link className="w-fit text-sm underline" to={`/stocks/stock-assessments/${item.assessment_ref.assessment_id}`}>この提案が参照した評価を見る →</Link> : <p className="text-sm text-muted-foreground">調査未完了のため、完成評価への参照はありません。</p>}
  </article>
}

export function ProposalAssessmentDetails({ item }: { item: InvestmentProposalItemView }) {
  if (!item.assessment_ref) return null
  const projection = record(item.assessment_projection)
  const document = record(projection.raw)
  return <details className="scroll-mt-24 rounded-lg border bg-card">
    <summary className="cursor-pointer px-4 py-3 font-medium">{item.ticker} · 提案が参照した評価と Review</summary>
    <SectionCard padded title={`${item.ticker} · 保存評価の根拠`} description="この提案が参照した評価版です。最新評価へ付け替えません。">
      <p className="mb-4 break-all text-xs">Assessment: {item.assessment_ref.assessment_id} · Review: {item.assessment_ref.review_id}</p>
      {Object.keys(document).length > 0 ? <ReviewedAssessmentContent document={document} projection={projection} /> : <p>参照評価の本文を表示できません。保存 ID から評価を確認してください。</p>}
      <details className="mt-4 rounded border p-4"><summary className="cursor-pointer text-sm">参照した独立 Review</summary>{item.assessment_review ? <pre className="mt-3 overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify(item.assessment_review, null, 2)}</pre> : <p className="mt-3 text-sm">参照した Review 本文を表示できません。保存 ID から確認してください。</p>}</details>
      <Link className="mt-4 inline-block text-sm underline" to={`/stocks/stock-assessments/${item.assessment_ref.assessment_id}`}>保存した評価版を開く →</Link>
    </SectionCard>
  </details>
}

export function CurrentPreviewContent({ preview }: { preview: InvestmentProposalPreviewView }) {
  const inputs = record(preview.current_inputs)
  return <div className="grid gap-3">
    <p className="text-sm">再確認時点 {formatJstStamp(preview.previewed_at)} · {{ unchanged: '保存条件から変化なし', changed: '入力または条件に変化あり', blocked: '現在条件では提案条件を満たせません' }[preview.status]}</p>
    <p className="text-sm">現在の利用可能 cash {text(inputs.available_cash_yen)}円</p>
    {preview.change_reasons.length > 0 && <ul className="list-inside list-disc text-sm">{preview.change_reasons.map((reason, index) => <li key={`${reason.code}-${index}`}>{reason.message}</li>)}</ul>}
    {preview.items.map((item, index) => {
      const current = record(item.current_quote_calculation)
      const revalidation = record(item.saved_limit_revalidation)
      const projection = item.saved_action === 'no_action' ? current : record(current.current_price_projection)
      const unavailableReason = current.return_calculation_unavailable_reason
      const base = record(projection.base)
      const downside = record(projection.downside)
      const blockers = Array.isArray(item.blockers) ? item.blockers.map(record) : []
      return <article className="grid gap-2 rounded border p-3" key={`${item.ticker}-${index}`}><p className="font-mono text-sm">{text(item.ticker)}</p>
        <div className="grid gap-3 sm:grid-cols-2"><div><p className="text-sm font-medium">現在 quote での再試算</p>{Object.keys(projection).length === 0 ? <p className="text-sm">再試算できません。{typeof unavailableReason === 'string' && (RETURN_UNAVAILABLE_LABELS[unavailableReason] ?? unavailableReason)}</p> : <><p className="text-sm">試算価格 {text(current.calculation_price_yen ?? projection.price_yen)}円</p><p className="text-xs">Base {text(base.total_return_pct)}% · Downside {text(downside.total_return_pct)}%</p></>}</div>
        <div><p className="text-sm font-medium">保存 limit の再検証</p>{Object.keys(revalidation).length === 0 ? <p className="text-sm">対象なし、または条件未確定。</p> : <p className="text-sm">{item.saved_action === 'sell' ? '保存した売却数量' : '現在資金等での試算数量'} {text(revalidation.quantity)}株</p>}</div></div>
        {blockers.map((reason, i) => <p className="text-sm" key={i}>{text(reason.message)}</p>)}
      </article>
    })}
    {preview.warnings.length > 0 && <p className="break-words text-xs text-muted-foreground">現在の再試算に対する警告: {preview.warnings.map(proposalWarningLabel).join(' / ')}</p>}
    <p className="text-xs text-muted-foreground">保存した価格・数量・期限を更新していません。改めて勧めるには新しい提案と Review が必要です。</p>
  </div>
}

function CurrentProposalPreviewPanel({ proposalId }: { proposalId: string }) {
  const [preview, setPreview] = useState<InvestmentProposalPreviewView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const controller = useRef<AbortController | null>(null)
  useEffect(() => () => controller.current?.abort(), [])
  const refresh = () => {
    controller.current?.abort()
    const request = new AbortController()
    controller.current = request
    setLoading(true)
    setError(null)
    fetchJson<InvestmentProposalPreviewView>(`/api/investment-proposals/${encodeURIComponent(proposalId)}/current-preview`, { signal: request.signal })
      .then(result => { if (!request.signal.aborted) setPreview(result) })
      .catch((reason: unknown) => {
        if (request.signal.aborted || (reason instanceof Error && reason.name === 'AbortError')) return
        setError(reason instanceof Error ? reason.message : '公開済みの再確認結果を読み込めませんでした')
      })
      .finally(() => { if (!request.signal.aborted) setLoading(false) })
  }
  return <SectionCard padded title="現在の条件を別に確認" description="公開サイトでは、最終データ生成時点の再試算結果を再読込します。クリック時には再計算しません。保存条件は変更しません。">
    <Button disabled={loading} onClick={refresh} variant="outline">{loading ? '再確認中' : '公開済みの再確認結果を読み込む'}</Button>
    {error && <p className="mt-3 text-sm text-destructive" role="alert">{error}</p>}
    {preview && <div className="mt-4"><CurrentPreviewContent preview={preview} /></div>}
  </SectionCard>
}

export function InvestmentProposalPage() {
  const { proposalId = '' } = useParams<{ proposalId: string }>()
  const [data, setData] = useState<InvestmentProposalView | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    setData(null)
    setError(null)
    fetchJson<InvestmentProposalView>(`/api/investment-proposals/${encodeURIComponent(proposalId)}`, { signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) setData(result) })
      .catch((reason: unknown) => {
        if (controller.signal.aborted || (reason instanceof Error && reason.name === 'AbortError')) return
        setError(reason instanceof Error ? reason.message : '投資提案を読み込めませんでした')
      })
    return () => controller.abort()
  }, [proposalId])
  if (error) return <PageState message={error} title="投資提案" />
  if (!data) return <LoadingPage label="投資提案を読み込んでいます" />
  const quotes = Array.isArray(data.saved_inputs.quotes) ? data.saved_inputs.quotes.map(record) : []
  return <PageShell above={<ResearchNav />} title={data.headline} lead="発行時の評価・比較範囲・価格・資金を固定した提案です。採否・注文・約定は別工程です。" width="reading" meta={<div className="flex flex-wrap gap-3"><AsOfBadge value={data.as_of} /><UpdatedAtBadge value={data.published_at} /></div>}>
    <SectionCard padded title="比較と今回の判断"><p className="whitespace-pre-line text-sm">{data.comparison}</p><p className="mt-3 whitespace-pre-line text-sm">見送った選択肢: {data.forgone}</p></SectionCard>
    {data.items.map(item => <ProposalItemContent item={item} savedQuote={quotes.find(quote => quote.ticker === item.ticker)} key={item.ticker} />)}
    {data.unfinished_research.length > 0 && <SectionCard padded title="調査未完了" description="完成した経済的な見送りと区別した、提案当時の不足です。">{data.unfinished_research.map(item => <div className="mb-3 text-sm" key={item.ticker}><p>{item.ticker} · {item.unfinished_reason}</p><p className="break-all text-xs text-muted-foreground">{item.references.join(' · ')}</p></div>)}</SectionCard>}
    <SectionCard padded title="発行時の資金・範囲・警告"><p className="text-sm">保存した利用可能 cash {text(data.saved_inputs.available_cash_yen)}円 · 入力確認時点 {text(data.saved_inputs.checked_at)}</p>{data.warnings.length > 0 && <p className="mt-2 break-words text-xs">当時の警告: {data.warnings.map(proposalWarningLabel).join(' / ')}</p>}<details className="mt-3"><summary className="cursor-pointer text-sm">保存した入力・比較範囲・資本集中</summary><pre className="mt-3 overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify({ inputs: data.saved_inputs, research_scope: data.research_scope, portfolio_exposure: data.portfolio_exposure, liquidity_context: data.liquidity_context }, null, 2)}</pre></details></SectionCard>
    <CurrentProposalPreviewPanel key={data.proposal_id} proposalId={data.proposal_id} />
    {data.items.map(item => <ProposalAssessmentDetails item={item} key={item.ticker} />)}
    <details className="rounded border p-4"><summary className="cursor-pointer text-sm">提案内容の独立 Review</summary><pre className="mt-3 overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify(data.review, null, 2)}</pre><p className="mt-3 break-all text-xs">Proposal: {data.proposal_id} · draft hash: {data.draft_sha256}</p></details>
    {data.market_context_assessment_id && <Link className="text-sm underline" to={`/market-context/assessments/${data.market_context_assessment_id}`}>この提案が参照した市場環境評価を見る →</Link>}
    <Link className="text-sm underline" to="/stocks">提案一覧に戻る</Link>
  </PageShell>
}
