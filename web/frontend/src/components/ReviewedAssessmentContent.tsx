import type { ReactNode } from 'react'
import { researchRecord as record, researchText as text } from '../lib/research'

function Field({ label, children }: { label: string; children: ReactNode }) {
  return <div className="grid gap-1"><dt className="text-xs font-medium text-muted-foreground">{label}</dt><dd className="text-sm font-medium">{children}</dd></div>
}

export function ReviewedAssessmentContent({ document, projection }: { document: Record<string, unknown>; projection: Record<string, unknown> }) {
  const investment = record(document.investment_case)
  const findings = record(document.findings)
  const realization = record(findings.realization)
  const conclusion = record(document.conclusion)
  const valuation = record(document.valuation)
  const snapshot = record(document.input_snapshot)
  const projections = record(projection.projections)
  return <div className="grid gap-4">
    <Field label="事業の収益構造"><p>{text(findings.business_understanding)}</p></Field>
    <Field label="価値と価格の隔たり"><p>{text(findings.value_gap_explanation)}</p></Field>
    <Field label="価値実現の道筋"><p>{text(realization.mechanism)}</p><p>時期 {text(realization.timing)} · {text(realization.timing_reason)}</p><p>長期化のリスク: {text(realization.delay_risk)}</p></Field>
    <Field label="投資仮説"><p>{text(investment.explanation)}</p></Field>
    <Field label="投資理由の成立性"><p>{text(investment.status)} — {text(investment.status_reason)}</p></Field>
    <Field label="重大な不成立条件"><p>{Array.isArray(investment.invalidation_conditions) ? investment.invalidation_conditions.join(' / ') : '未確認'}</p></Field>
    <Field label="最も強い反対仮説"><p>{text(conclusion.strongest_countercase)}</p></Field>
    {Array.isArray(findings.hypotheses) && findings.hypotheses.length > 0 && <Field label="検証した仮説"><div className="grid gap-2">{findings.hypotheses.map((value, index) => {
      const hypothesis = record(value)
      return <p key={index}>{text(hypothesis.statement)} · {text(hypothesis.result)} · {text(hypothesis.reason)}</p>
    })}</div></Field>}
    {Array.isArray(findings.unresolved_questions) && findings.unresolved_questions.length > 0 && <Field label="未解決の問い"><p>{findings.unresolved_questions.join(' / ')}</p></Field>}
    {valuation.status === 'unresolved' && <Field label="価値見積り未解決の理由"><p>{text(valuation.unresolved_reason)}</p></Field>}
    <p className="text-sm">評価起点 {text(snapshot.as_of)} / 原評価の観測価格 {text(projection.original_price_yen)} 円 / 日時 {text(projection.original_quote_at)} / basis {text(projection.original_price_basis)}</p>
    <p className="text-sm">期間 {text(valuation.horizon_months)} か月 / 要求年率 {text(valuation.required_annual_return_pct)}%</p>
    <div className="grid gap-4 sm:grid-cols-2">{['base', 'downside'].map(name => {
      const arithmetic = record(projections[name])
      const assumptions = record(valuation[name])
      return <div className="rounded border p-4" key={name}><strong>{name === 'base' ? 'Base' : 'Downside'}</strong>
        <p>分配後の価値 {text(assumptions.terminal_value_per_share_yen)} 円 / 今後の累積分配 {text(assumptions.cash_distribution_per_share_yen)} 円</p>
        <p>原評価価格の条件付き総 return {text(arithmetic.total_return_pct)}% / 年率換算 {text(arithmetic.annualized_return_pct)}%</p>
        <p className="mt-2 text-sm">{text(assumptions.calculation)}</p></div>
    })}</div>
    <p className="text-sm text-muted-foreground">条件付き・税費用控除前・分配再投資なし。現在の購入判断は提案で確認します。</p>
    <details className="rounded border p-4"><summary className="cursor-pointer font-medium">Stock Assessment 本文・入力・出典</summary><pre className="mt-3 overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify(document, null, 2)}</pre></details>
  </div>
}
