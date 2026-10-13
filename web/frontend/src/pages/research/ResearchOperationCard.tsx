import { Link } from 'react-router'
import type { OperationSessionView } from '../../api/types'
import { AsOfBadge } from '../../components/AsOfBadge'
import { SectionCard } from '../../components/SectionCard'
import { Badge } from '../../components/ui/badge'
import { formatJstStamp } from '../../lib/format'

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
}
function records(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.flatMap(item => { const row = record(item); return row ? [row] : [] }) : []
}
export function HoldingProposals({ operations, names = new Map() }: { operations: OperationSessionView[]; names?: ReadonlyMap<string, string | null> }) {
  const proposals = operations.flatMap(operation => records(records(operation.payload.artifacts).find(item => item.kind === 'research_progress')?.outcomes).filter(item => item.status === 'proposal').map(outcome => ({ operation, outcome })))
  if (proposals.length === 0) return null
  return <SectionCard padded title="保有の見直し案" description="保存した保有案です。人間の採否・注文・約定は別工程です。">
    <div className="grid gap-3">{proposals.map(({ operation, outcome }) => {
      const draft = record(outcome.draft)
      const quote = record(draft?.quote)
      const reward = record(draft?.remaining_reward)
      const ticker = String(outcome.ticker)
      return <article className="grid scroll-mt-24 gap-2 rounded-lg border p-4" id={`holding-proposal-${operation.operation_id}-${ticker}`} key={`${operation.operation_id}-${ticker}`}>
        <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="font-medium">{names.get(ticker) ?? ticker}</h2><Badge variant="outline">{draft?.action === 'exit' ? '売却の提案' : draft?.action === 'hold' ? '継続保有の提案' : '保有判断未確定'}</Badge></div>
        {reward?.reason != null && <p className="text-sm">{String(reward.reason)}</p>}
        {draft?.unresolved_reason != null && <p className="text-sm">未確定の理由: {String(draft.unresolved_reason)}</p>}
        <p className="text-xs text-muted-foreground">検証時点 {String(outcome.checked_at ?? '未記録')}</p>
        <p className="break-words text-xs text-muted-foreground">{quote ? `保存した価格 ${String(quote.price_yen ?? '未記録')}円 · ${String(quote.price_basis ?? '未記録')} · ${String(quote.observed_at ?? '未記録')}` : '価格の入力は記録されていません。'}</p>
        <details className="text-xs text-muted-foreground"><summary className="cursor-pointer">保存した判断の参照</summary><p className="mt-2 break-all">Thesis: {String(draft?.thesis_id ?? '未記録')} · Review: {String(outcome.thesis_review_id ?? '未記録')} · Operation: {operation.operation_id}</p></details>
        <Link className="w-fit text-xs underline" to={`/securities/${ticker}`}>最新の銘柄情報を見る →</Link>
      </article>
    })}</div>
  </SectionCard>
}
export function ResearchOperationCard({ operations, names = new Map() }: { operations: OperationSessionView[]; names?: ReadonlyMap<string, string | null> }) {
  const sessions = operations.flatMap(operation => {
    const artifacts = records(operation.payload.artifacts)
    const selection = artifacts.find(item => item.kind === 'research_selection')
    return selection ? [{ operation, selection, artifacts, outcomes: records(artifacts.find(item => item.kind === 'research_progress')?.outcomes) }] : []
  }).sort((a, b) => Number(b.operation.status === 'active') - Number(a.operation.status === 'active') || b.operation.started_at.localeCompare(a.operation.started_at))
  const content = sessions.map(({ operation, selection, artifacts, outcomes }) => {
    const targets = records(selection.targets)
    const proposed = outcomes.some(item => item.status === 'proposal') || artifacts.some(item => item.kind === 'capital_allocation_assessment')
    const complete = operation.status === 'completed'
    return <article className="grid gap-3 rounded-lg border p-4" key={operation.operation_id}>
      <div className="flex flex-wrap items-center justify-between gap-2"><p className="font-medium">{!complete ? '調査進行中' : proposed ? '提案工程完了' : outcomes.some(item => item.status === 'reviewed') ? '企業調査終了' : '提案なしで終了'}</p><AsOfBadge value={operation.as_of} /></div>
      <p className="text-xs text-muted-foreground">開始 {formatJstStamp(operation.started_at)} · 固定対象 {targets.length}件</p>
      {targets.length === 0 && <p className="text-sm">今回の調査対象は0件です。対象が選ばれなかった理由は記録で確認できます。</p>}
      {targets.map(target => {
        const ticker = String(target.ticker)
        const outcome = outcomes.find(item => item.ticker === target.ticker)
        const draft = record(outcome?.draft)
        const state = outcome?.status === 'reviewed' ? '企業評価・レビュー完了' : outcome?.status === 'proposal' ? `保有提案 ${draft?.action === 'hold' ? 'hold' : draft?.action === 'exit' ? 'exit' : '未確定'}（人間確認済み公開とは別）` : outcome?.status === 'unfinished' ? `調査未完了: ${String(outcome.reason)}` : '調査中'
        return <div className="grid gap-2 border-t pt-3 text-sm" key={ticker}>
          <p className="font-medium">{names.get(ticker) ?? ticker}<span className="ml-2 text-xs text-muted-foreground">{target.purpose === 'holding' ? '保有の見直し' : '新規の企業調査'}</span></p>
          <p>{state}</p>
          {outcome?.status === 'reviewed' && <p className="text-xs text-muted-foreground">企業評価の完了です。配分の提案・採否・約定を意味しません。</p>}
          {outcome?.status === 'proposal' && <a className="w-fit text-xs underline" href={`#holding-proposal-${operation.operation_id}-${ticker}`}>保存した保有案・価格・判断根拠を見る →</a>}
          <Link className="w-fit text-xs underline" to={`/securities/${ticker}`}>最新の銘柄情報を見る →</Link>
        </div>
      })}
      <details className="text-xs text-muted-foreground"><summary className="cursor-pointer">今回の記録・参照</summary><div className="mt-2 grid gap-2 break-words"><p>{String(selection.request ?? '')}</p><p>{String(operation.payload.result ?? operation.payload.checkpoint)}</p>{operation.payload.next != null && <p>次の確認: {String(operation.payload.next)}</p>}<p className="break-all">Operation: {operation.operation_id}</p>{outcomes.map(item => <p className="break-all" key={String(item.ticker)}>{String(item.ticker)} · {String(item.thesis_id ?? record(item.draft)?.thesis_id ?? '')}{Array.isArray(item.references) && ` · ${item.references.join(' · ')}`}</p>)}</div></details>
    </article>
  })
  return <SectionCard padded title="調査と提案の進行" description="新規・保有を合わせた固定対象の記録。未完了は投資判断としての見送りと区別します。">
    {content.length === 0 ? <p className="text-sm text-muted-foreground">有限調査の進行記録はまだありません。</p> : <div className="grid gap-4">{content.slice(0, 3)}{content.length > 3 && <details><summary className="cursor-pointer text-sm">以前の調査記録（{content.length - 3}件）</summary><div className="mt-4 grid gap-4">{content.slice(3)}</div></details>}</div>}
  </SectionCard>
}
