import type { ReactNode } from 'react'
import { researchRecord as record, researchText as text } from '../lib/research'

function Field({ label, children }: { label: string; children: ReactNode }) {
  return <dl className="grid gap-1"><dt className="text-xs font-medium text-muted-foreground">{label}</dt><dd className="text-sm font-medium">{children}</dd></dl>
}

function SourceRefs({ ids, sources }: { ids: unknown; sources: Record<string, unknown>[] }) {
  if (!Array.isArray(ids) || ids.length === 0) return null
  return <p className="text-xs text-muted-foreground">参照: {ids.map((value, index) => {
    const id = text(value)
    const source = sources.find(item => item.source_id === id)
    const label = `${id}${typeof source?.as_of === 'string' ? ` · 保存sourceのas_of ${source.as_of}` : ''}`
    return <span key={`${id}-${index}`}>{index > 0 && ' / '}{typeof source?.ref === 'string' && source.ref.startsWith('https://')
      ? <a className="underline" href={source.ref} rel="noopener noreferrer" target="_blank">{label}</a>
      : label}</span>
  })}</p>
}

export function ReviewedAssessmentContent({ document, projection, review }: { document: Record<string, unknown>; projection: Record<string, unknown>; review?: Record<string, unknown> | null }) {
  const investment = record(document.investment_case)
  const findings = record(document.findings)
  const realization = record(findings.realization)
  const conclusion = record(document.conclusion)
  const valuation = record(document.valuation)
  const snapshot = record(document.input_snapshot)
  const sources = Array.isArray(snapshot.sources) ? snapshot.sources.map(record) : []
  const projections = record(projection.projections)
  const independentReview = record(review)
  return <div className="grid gap-5">
    <section className="grid gap-3" aria-label="価値を生む仮説">
      <h3 className="text-sm font-semibold">価値を生む仮説</h3>
      <Field label="事業の収益構造"><p>{text(findings.business_understanding)}</p><SourceRefs ids={findings.source_ids} sources={sources} /></Field>
      <Field label="価値と価格の隔たり"><p>{text(findings.value_gap_explanation)}</p></Field>
      <Field label="投資仮説"><p>{text(investment.explanation)}</p><SourceRefs ids={investment.source_ids} sources={sources} /></Field>
      <Field label="投資理由の成立性"><p>{text(investment.status)} — {text(investment.status_reason)}</p></Field>
    </section>
    <section className="grid gap-3 border-t pt-4" aria-label="観測と個社への伝達">
      <h3 className="text-sm font-semibold">観測と個社への伝達</h3>
      {Array.isArray(findings.hypotheses) && findings.hypotheses.length > 0 && <Field label="検証した仮説・相違"><div className="grid gap-2">{findings.hypotheses.map((value, index) => {
        const hypothesis = record(value)
        return <div key={index}><p>{text(hypothesis.statement)} · {text(hypothesis.result)} · {text(hypothesis.reason)}</p><SourceRefs ids={hypothesis.source_ids} sources={sources} /></div>
      })}</div></Field>}
      {Array.isArray(findings.unresolved_questions) && findings.unresolved_questions.length > 0 && <Field label="未解決の問い"><p>{findings.unresolved_questions.join(' / ')}</p></Field>}
    </section>
    <section className="grid gap-3 border-t pt-4" aria-label="条件付き価値と時間">
      <h3 className="text-sm font-semibold">条件付き価値と時間</h3>
      <Field label="価値実現の道筋"><p>{text(realization.mechanism)}</p><p>時期 {text(realization.timing)} · {text(realization.timing_reason)}</p><p>長期化のリスク: {text(realization.delay_risk)}</p></Field>
      {valuation.status === 'unresolved' && <Field label="価値見積り未解決の理由"><p>{text(valuation.unresolved_reason)}</p></Field>}
      <p className="text-sm">評価起点 {text(snapshot.as_of)} / 原評価の観測価格 {text(projection.original_price_yen)} 円 / 日時 {text(projection.original_quote_at)} / basis {text(projection.original_price_basis)}</p>
      {valuation.status === 'resolved' && <><p className="text-sm">期間 {text(valuation.horizon_months)} か月 / 要求年率 {text(valuation.required_annual_return_pct)}%</p>
      <div className="grid gap-4 sm:grid-cols-2">{['base', 'downside'].map(name => {
        const arithmetic = record(projections[name])
        const assumptions = record(valuation[name])
        return <div className="rounded border p-4" key={name}><strong>{name === 'base' ? 'Base' : 'Downside'}</strong>
          <p>分配後の価値 {text(assumptions.terminal_value_per_share_yen)} 円 / 今後の累積分配 {text(assumptions.cash_distribution_per_share_yen)} 円</p>
          <p>原評価価格の条件付き総 return {text(arithmetic.total_return_pct)}% / 年率換算 {text(arithmetic.annualized_return_pct)}%</p>
          <p className="mt-2 text-sm">{text(assumptions.calculation)}</p>
          <SourceRefs ids={assumptions.source_ids} sources={sources} />
        </div>
      })}</div>
      <p className="text-sm text-muted-foreground">条件付き・税費用控除前・分配再投資なし。現在の購入判断は提案で確認します。</p></>}
    </section>
    <section className="grid gap-3 border-t pt-4" aria-label="反証と独立Review">
      <h3 className="text-sm font-semibold">反証と独立Review</h3>
      <Field label="重大な不成立条件"><p>{Array.isArray(investment.invalidation_conditions) ? investment.invalidation_conditions.join(' / ') : '未確認'}</p></Field>
      <Field label="作者の最も強い反対仮説"><p>{text(conclusion.strongest_countercase)}</p></Field>
      {review && <><Field label="独立Reviewの最も強い反対仮説"><p>{text(independentReview.strongest_countercase)}</p></Field><Field label="独立Reviewの確認範囲"><p>{text(independentReview.review_scope)}</p><SourceRefs ids={independentReview.checked_source_ids} sources={sources} /></Field></>}
    </section>
    <details className="rounded border p-4"><summary className="cursor-pointer font-medium">Stock Assessment 本文・入力・出典</summary><pre className="mt-3 overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify(document, null, 2)}</pre></details>
  </div>
}
