import type { ReactNode } from 'react'

function Field({ label, children }: { label: string; children: ReactNode }) {
  return <div className="grid gap-1"><dt className="text-xs font-medium text-muted-foreground">{label}</dt><dd className="text-sm font-medium">{children}</dd></div>
}

export function ReviewedThesisContent({ projection }: { projection: Record<string, unknown> }) {
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
    {projection.raw != null && <details className="rounded border p-4"><summary className="cursor-pointer font-medium">Thesis本文・入力・出典</summary><pre className="mt-3 overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify(projection.raw, null, 2)}</pre></details>}
  </div>
}
