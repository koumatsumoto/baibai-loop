import type { OperationSessionView } from '../../api/types'
import { SectionCard } from '../../components/SectionCard'

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
}
function records(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.flatMap(item => { const row = record(item); return row ? [row] : [] }) : []
}
export function ResearchOperationCard({ operations }: { operations: OperationSessionView[] }) {
  const sessions = operations.flatMap(operation => {
    const artifacts = records(operation.payload.artifacts)
    const selection = artifacts.find(item => item.kind === 'research_selection')
    return selection ? [{ operation, selection, outcomes: records(artifacts.find(item => item.kind === 'research_progress')?.outcomes) }] : []
  }).slice(0, 3)
  if (sessions.length === 0) return null
  return <SectionCard padded title="調査と提案の進行" description="固定した新規・保有対象を調査します。提案完成と人間の採否・注文は別工程です。">
    <div className="grid gap-4">{sessions.map(({ operation, selection, outcomes }) => {
      const targets = records(selection.targets)
      const proposed = outcomes.some(item => item.status === 'reviewed' || item.status === 'proposal')
      return <div key={operation.operation_id} className="text-sm"><p className="font-medium">{operation.status === 'active' ? '調査進行中' : proposed ? '提案工程完了' : '提案なしで終了'} · 固定対象 {targets.length}件</p>
        {targets.map(target => {
          const outcome = outcomes.find(item => item.ticker === target.ticker)
          const draft = record(outcome?.draft)
          const state = outcome?.status === 'reviewed' ? 'Reviewed Thesis完了' : outcome?.status === 'proposal' ? `保有提案 ${draft?.action === 'hold' ? 'hold' : draft?.action === 'exit' ? 'exit' : '未確定'}（人間確認済み公開とは別）` : outcome?.status === 'unfinished' ? `調査未完了: ${String(outcome.reason)}` : '調査中'
          return <p key={String(target.ticker)} className="mt-1"><span className="font-mono">{String(target.ticker)}</span> · {target.purpose === 'holding' ? '保有' : '新規'} · {state}</p>
        })}
        <p className="mt-1 break-all text-xs text-muted-foreground">{operation.operation_id} · {String(operation.payload.result ?? operation.payload.checkpoint)}</p>
      </div>
    })}</div>
  </SectionCard>
}
