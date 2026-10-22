import type { ReactNode } from 'react'
import { STANCE_LABELS, researchRecord as record, researchReturnPct, researchText as text } from '../lib/research'

const RISK_AXIS_LABELS: Readonly<Record<string, string>> = {
  funding_liquidity: '資金繰り', debt_repayment: '債務返済', cash_flow: 'キャッシュフロー',
  dilution: '希薄化', customer_concentration: '顧客集中', structural_decline: '構造的な衰退',
  governance_accounting: 'ガバナンス・会計',
}
const RISK_ASSESSMENT_LABELS: Readonly<Record<string, string>> = {
  acceptable: '許容できる', adverse: '不利', unknown: '未確認',
}
const EVIDENCE_LABELS: Readonly<Record<string, string>> = {
  verified: '確認済み', partially_verified: '一部確認', unverified: '未確認',
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return <dl className="grid min-w-0 gap-1"><dt className="text-xs font-medium text-muted-foreground">{label}</dt><dd className="whitespace-pre-line break-words text-sm leading-7">{children}</dd></dl>
}

function SourceRefs({ ids, sources, label = '参照:' }: { ids: unknown; sources: Record<string, unknown>[]; label?: string }) {
  if (!Array.isArray(ids) || ids.length === 0) return null
  return <p className="break-words text-xs text-muted-foreground">{label} {ids.map((value, index) => {
    const id = text(value)
    const source = sources.find(item => item.source_id === id)
    const label = `${typeof source?.used_for === 'string' ? `${source.used_for} · ` : ''}${id}${typeof source?.as_of === 'string' ? ` · 保存sourceのas_of ${source.as_of}` : ''}`
    return <span key={`${id}-${index}`}>{index > 0 && ' / '}{typeof source?.ref === 'string' && source.ref.startsWith('https://')
      ? <a className="underline" href={source.ref} rel="noopener noreferrer" target="_blank">{label}</a>
      : label}</span>
  })}</p>
}

export function AssessmentReviewContent({ review, sources = [] }: { review?: Record<string, unknown> | null; sources?: Record<string, unknown>[] }) {
  if (!review) return <p className="text-sm text-muted-foreground">独立Review本文を表示できません。保存した評価版のReviewを確認してください。</p>
  const recalculated = Array.isArray(review.recalculated_projections) ? review.recalculated_projections.map(record) : []
  return <div className="grid gap-4">
    <Field label="独立Reviewの確認範囲"><p>{text(review.review_scope)}</p></Field>
    <Field label="一次資料の確認状態"><p>{EVIDENCE_LABELS[text(review.primary_source_check)] ?? text(review.primary_source_check)}</p><SourceRefs ids={review.checked_source_ids} sources={sources} /></Field>
    <Field label="独立Reviewの最も強い反対仮説"><p>{text(review.strongest_countercase)}</p></Field>
    {review.nonmaterial_unknown_reason != null && <Field label="残る不明点を非重要とした理由"><p>{text(review.nonmaterial_unknown_reason)}</p></Field>}
    <Field label="独立再計算">
      {recalculated.length > 0 ? <div className="grid gap-3 sm:grid-cols-2">{recalculated.map((row, index) => <div className="rounded border p-3" key={index}>
        <p className="font-medium">{row.name === 'base' ? 'Base' : row.name === 'downside' ? 'Downside' : text(row.name)}</p>
        <p>分配後の価値 {text(row.terminal_value_per_share_yen)} 円</p>
        <p>今後の累積分配 {text(row.cash_distribution_per_share_yen)} 円</p>
      </div>)}</div> : <p>保存された再計算値はありません。価値未解決や確認範囲を上で確認してください。</p>}
    </Field>
    <p className="break-words text-xs text-muted-foreground">確認者 {text(review.reviewer_identity)} · 確認時刻 {text(review.reviewed_at)}</p>
  </div>
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
  const risks = Array.isArray(document.permanent_loss_risks) ? document.permanent_loss_risks.map(record) : []
  const hypotheses = Array.isArray(findings.hypotheses) ? findings.hypotheses : []
  const unresolvedQuestions = Array.isArray(findings.unresolved_questions) ? findings.unresolved_questions : []
  const hasHypotheses = hypotheses.length > 0
  const observationTitle = hasHypotheses ? '観測と個社への伝達' : '残る確認事項'
  return <div className="grid min-w-0 gap-6 break-words leading-relaxed">
    <section className="grid gap-3" aria-label="価値を生む仮説">
      <h3 className="text-sm font-semibold">価値を生む仮説</h3>
      <Field label="企業評価の結論"><p>{STANCE_LABELS[text(conclusion.stance)] ?? text(conclusion.stance)}</p></Field>
      <Field label="価値と価格の隔たり"><p>{text(findings.value_gap_explanation)}</p></Field>
      <Field label="事業の収益構造"><p>{text(findings.business_understanding)}</p></Field>
      <Field label="投資仮説"><p>{text(investment.explanation)}</p><SourceRefs ids={investment.source_ids} sources={sources} /></Field>
      <Field label="投資仮説の成立性"><p>{text(investment.status)} — {text(investment.status_reason)}</p></Field>
    </section>
    <SourceRefs ids={findings.source_ids} sources={sources} label="所見全体の参照:" />
    {(hasHypotheses || unresolvedQuestions.length > 0) && <section className="grid gap-3 border-t pt-4" aria-label={observationTitle}>
      <h3 className="text-sm font-semibold">{observationTitle}</h3>
      {hasHypotheses && <Field label="検証した仮説・相違"><div className="grid gap-2">{hypotheses.map((value, index) => {
        const hypothesis = record(value)
        return <div key={index}><p>{text(hypothesis.statement)} · {text(hypothesis.result)} · {text(hypothesis.reason)}</p><SourceRefs ids={hypothesis.source_ids} sources={sources} /></div>
      })}</div></Field>}
      {unresolvedQuestions.length > 0 && <Field label="未解決の問い"><p>{unresolvedQuestions.join(' / ')}</p></Field>}
    </section>}
    <section className="grid gap-3 border-t pt-4" aria-label="条件付き価値と時間">
      <h3 className="text-sm font-semibold">条件付き価値と時間</h3>
      <Field label="価値実現の道筋"><p>{text(realization.mechanism)}</p><p>時期 {text(realization.timing)} · {text(realization.timing_reason)}</p><p>長期化のリスク: {text(realization.delay_risk)}</p></Field>
      {valuation.status === 'unresolved' && <Field label="価値見積り未解決の理由"><p>{text(valuation.unresolved_reason)}</p></Field>}
      <p className="text-sm">評価起点 {text(snapshot.as_of)} / 原評価の観測価格 {text(projection.original_price_yen)} 円 / 価格基準日 {text(projection.original_price_as_of)}{projection.original_quote_at != null && <> / 観測時刻 {text(projection.original_quote_at)}</>} / basis {text(projection.original_price_basis)}</p>
      {valuation.status === 'resolved' && <><p className="text-sm">期間 {text(valuation.horizon_months)} か月 / 要求年率 {text(valuation.required_annual_return_pct)}%</p>
      <div className="grid gap-4 sm:grid-cols-2">{['base', 'downside'].map(name => {
        const arithmetic = record(projections[name])
        const assumptions = record(valuation[name])
        return <div className="rounded border p-4" key={name}><strong>{name === 'base' ? 'Base' : 'Downside'}</strong>
          <p>分配後の価値 {text(assumptions.terminal_value_per_share_yen)} 円 / 今後の累積分配 {text(assumptions.cash_distribution_per_share_yen)} 円</p>
          <p>原評価価格の条件付き総 return {researchReturnPct(arithmetic.total_return_pct)} / 年率換算 {researchReturnPct(arithmetic.annualized_return_pct)}</p>
          <p className="mt-2 text-sm">{text(assumptions.calculation)}</p>
          <SourceRefs ids={assumptions.source_ids} sources={sources} />
        </div>
      })}</div>
      <p className="text-sm text-muted-foreground">条件付き・税費用控除前・分配再投資なし。現在の購入判断は提案で確認します。</p></>}
    </section>
    <section className="grid gap-3 border-t pt-4" aria-label="永久損失リスク">
      <h3 className="text-sm font-semibold">永久損失リスク</h3>
      <p className="text-xs text-muted-foreground">保存評価のリスク判断と根拠の確認状態です。確認済みは、リスクがないことを意味しません。</p>
      {risks.length > 0 ? <ul className="grid gap-3">{risks.map((risk, index) => <li className="grid gap-1" key={`${text(risk.axis)}-${index}`}>
        <p className="text-sm font-medium">{RISK_AXIS_LABELS[text(risk.axis)] ?? text(risk.axis)}</p>
        <p className="text-xs text-muted-foreground">判断: {RISK_ASSESSMENT_LABELS[text(risk.assessment)] ?? text(risk.assessment)} / 根拠: {EVIDENCE_LABELS[text(risk.evidence_status)] ?? text(risk.evidence_status)} / 評価基準日: {text(risk.as_of)}</p>
        <p className="whitespace-pre-line text-sm">{text(risk.summary)}</p>
        <SourceRefs ids={risk.source_ids} sources={sources} />
      </li>)}</ul> : <p className="text-sm text-muted-foreground">リスク本文を表示できません。保存した評価版を確認してください。</p>}
    </section>
    <section className="grid gap-3 border-t pt-4" aria-label="反証と独立Review">
      <h3 className="text-sm font-semibold">反証と独立Review</h3>
      <Field label="重大な不成立条件"><p>{Array.isArray(investment.invalidation_conditions) ? investment.invalidation_conditions.join(' / ') : '未確認'}</p></Field>
      <Field label="作者の最も強い反対仮説"><p>{text(conclusion.strongest_countercase)}</p></Field>
      <AssessmentReviewContent review={review} sources={sources} />
    </section>
    <details className="min-w-0 rounded border p-4"><summary className="min-h-11 cursor-pointer py-2 font-medium">保存した入力・出典・原文</summary>
      <div className="mt-3 grid gap-4">{sources.map((source, index) => <dl className="grid gap-1 text-sm" key={index}>
        <dt className="font-medium">{text(source.source_id)}</dt><dd>用途: {text(source.used_for)}</dd>
        <dd>対象時点: {text(source.as_of)} · 受領時刻: {text(source.retrieved_at)}</dd>
        <dd>{typeof source.ref === 'string' && source.ref.startsWith('https://') ? <a className="underline" href={source.ref} rel="noopener noreferrer" target="_blank">掲載元を開く</a> : <>{text(source.provider)} · {text(source.dataset)}</>}</dd>
      </dl>)}</div>
      <details className="mt-4"><summary className="min-h-11 cursor-pointer py-2 text-sm">Stock Assessment / Review の保存JSON</summary><pre className="mt-3 max-w-full overflow-auto whitespace-pre-wrap break-all text-xs">{JSON.stringify({ assessment: document, review: review ?? null }, null, 2)}</pre></details>
    </details>
  </div>
}
