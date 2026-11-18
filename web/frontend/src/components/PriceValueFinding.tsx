import type { SavedAssessmentView, SavedPriceValueView } from '../api/types'
import { formatJstDateTime } from '../lib/format'
import { AssessmentSources } from './AssessmentSources'

const labels: Record<SavedPriceValueView['status'], string> = {
  supported_at_quote: 'この価格で割安を支持',
  not_supported_at_quote: 'この価格では割安を支持しない',
  undetermined: '価値と価格の比較は未確定',
}

export function PriceValueFinding({ assessment }: { assessment: SavedAssessmentView | null }) {
  const finding = assessment?.price_value_case
  if (assessment == null || finding == null) {
    return <section aria-label="一株の価値と価格" className="text-sm text-muted-foreground">
      {assessment == null
        ? '一株の価値と価格：この提案に確認済みの評価はありません。'
        : '一株の価値と価格：この保存資料には明示的な割安判断がありません。'}
    </section>
  }
  const quote = finding.observed_quote
  const condition = finding.price_only_condition
  const sourceIds = [...new Set([...(quote?.source_ids ?? []), ...finding.value_facts.flatMap(fact => fact.source_ids)])]
  return <section aria-label="一株の価値と価格" className="grid min-w-0 gap-2 rounded-lg border p-4">
    <div>
      <p className="text-xs text-muted-foreground">一株の価値と価格 · 評価日 <time dateTime={assessment.as_of}>{assessment.as_of}</time></p>
      <h4 className="mt-1 text-base font-semibold">{condition == null ? labels[finding.status] : 'この価格では支持せず、価格条件を待つ'}</h4>
    </div>
    {quote != null ? <p className="text-sm tabular-nums">
      評価の参照価格：{quote.price_yen} 円／株（<time dateTime={quote.as_of}>{quote.as_of}</time>・{quote.price_basis === 'last_close_unadjusted' ? '未調整終値' : quote.price_basis === 'realtime' ? '時点値' : quote.price_basis}）
      {quote.observed_at != null && <> · 観測 <time dateTime={quote.observed_at}>{formatJstDateTime(quote.observed_at)} JST</time></>}
    </p> : <p className="text-sm text-muted-foreground">評価の参照価格は記録されていません。</p>}
    {finding.price_compatible_conditions != null && <p className="whitespace-pre-wrap break-words text-sm leading-6">この価格と両立する条件：{finding.price_compatible_conditions}</p>}
    <p className="whitespace-pre-wrap break-words text-sm leading-6">普通株主にとっての根拠：{assessment.investment_case_explanation}</p>
    {finding.acquisition_limit != null && <p className="text-sm leading-6">
      評価に含めた権利を最後に取得できる立会：{finding.acquisition_limit.last_eligible_trade_date == null
        ? '未確認' : <time dateTime={finding.acquisition_limit.last_eligible_trade_date}>{finding.acquisition_limit.last_eligible_trade_date}</time>}（入金日ではありません）
    </p>}
    {condition != null && <p className="whitespace-pre-wrap break-words text-sm leading-6">
      同じ権利・事業前提での価格条件：{condition.explanation}
      {condition.ceiling_yen == null ? '（金額の指定なし）' : `（条件上限 ${condition.ceiling_yen} 円／株。注文指値ではありません）`}
    </p>}
    <details className="text-sm">
      <summary className="cursor-pointer text-muted-foreground">何を確かめ、何が変われば見直すか</summary>
      <div className="mt-2 grid gap-2">
        {finding.value_facts.map(fact => <p className="break-words" key={fact.fact_id}>{fact.fact_id}：{fact.value}（{fact.unit}、<time dateTime={fact.as_of}>{fact.as_of}</time>）</p>)}
        {assessment.invalidation_conditions.map((condition, index) => <p className="whitespace-pre-wrap break-words" key={index}>反証条件：{condition}</p>)}
        <AssessmentSources sourceIds={sourceIds} sources={finding.sources} />
      </div>
    </details>
  </section>
}
