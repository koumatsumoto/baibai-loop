import { Link } from 'react-router'
import type { OperationSessionView } from '../../api/types'
import { AsOfBadge } from '../../components/AsOfBadge'
import { SectionCard } from '../../components/SectionCard'
import { formatJstStamp } from '../../lib/format'

const OPERATION_KIND_LABEL: Record<string, string> = {
  'capital-allocation': '新規購入の調査',
  'position-review': '保有の見直し',
}

export function InvestmentJudgmentOperationCard({ operations, names = new Map() }: { operations: OperationSessionView[]; names?: ReadonlyMap<string, string | null> }) {
  const sessions = [...operations].sort((a, b) => Number(b.status === 'active') - Number(a.status === 'active') || Date.parse(b.started_at) - Date.parse(a.started_at))
  const content = sessions.map(operation => {
    const research = operation.research
    if (!research) return <article className="grid gap-3 rounded-lg border p-4" key={operation.operation_id}>
      <div className="flex flex-wrap items-center justify-between gap-2"><p className="font-medium">{OPERATION_KIND_LABEL[operation.session_kind] ?? operation.session_kind}{operation.ticker && <span className="ml-2 font-mono text-xs text-muted-foreground">{operation.ticker}</span>}</p><AsOfBadge value={operation.as_of} /></div>
      <p className="text-sm">固定対象情報のない保存記録</p>
      <p className="text-sm">保存状態: {operation.status === 'active' ? '進行中' : operation.status === 'completed' ? '終了' : operation.status}</p>
      <p className="text-xs text-muted-foreground">開始 {formatJstStamp(operation.started_at)} · 対象数・成果の完成は判定しません。</p>
      <p className="break-all text-xs text-muted-foreground">Operation: {operation.operation_id}</p>
    </article>
    const reviewed = research.targets.filter(target => target.outcome_status === 'reviewed').length
    const unfinished = research.targets.filter(target => target.outcome_status === 'unfinished').length
    return <article className="grid gap-3 rounded-lg border p-4" key={operation.operation_id}>
      <div className="flex flex-wrap items-center justify-between gap-2"><p className="font-medium">{operation.status === 'completed' ? '調査終了' : operation.status === 'active' ? '調査進行中' : operation.status}</p><AsOfBadge value={operation.as_of} /></div>
      <p className="text-xs text-muted-foreground">開始 {formatJstStamp(operation.started_at)} · 固定対象 {research.targets.length}件</p>
      <p className="text-sm">銘柄評価・独立検証完了 {reviewed}件 · 調査未完了 {unfinished}件 · 成果未記録 {research.targets.length - reviewed - unfinished}件</p>
      <p className="text-sm">{research.proposal_id ? '保存提案あり。具体条件・保留・見送りは保存提案で確認します。' : '保存提案の参照はありません。'}</p>
      <p className="text-sm">{research.request_scope === 'discover_new' ? '新規購入の探索' : research.request_scope === 'specified_stocks' ? '指定銘柄の調査' : '保有の見直し'} · 深掘り上限 {research.max_deep_research_targets}件</p>
      {research.selection_summary && <p className="text-sm">{research.selection_summary}</p>}
      {research.targets.length === 0 && <p className="text-sm">今回の調査対象は0件です。対象が選ばれなかった理由は記録で確認できます。</p>}
      {research.targets.map(target => <div className="grid gap-2 border-t pt-3 text-sm" key={`${target.purpose}-${target.ticker}`}>
        <p className="font-medium">{names.get(target.ticker) ?? target.ticker}<span className="ml-2 text-xs text-muted-foreground">{target.purpose === 'holding' ? '保有の見直し' : '新規の企業調査'}</span></p>
        {target.selection_rationale && <p>選定理由: {target.selection_rationale}</p>}
        {target.research_question && <p>調査の問い: {target.research_question}</p>}
        {target.key_risk && <p>主要リスク: {target.key_risk}</p>}
        <p>{target.outcome_status === 'reviewed' ? '銘柄評価・独立検証完了' : target.outcome_status === 'unfinished' ? `調査未完了: ${target.unfinished_reason ?? '理由未記録'}` : '成果未記録'}</p>
        {target.outcome_status === 'reviewed' && <p className="text-xs text-muted-foreground">銘柄評価の完了です。提案・採否・約定は別に確認します。</p>}
        {target.assessment_ref && <Link className="w-fit text-xs underline" to={`/stocks/stock-assessments/${target.assessment_ref.assessment_id}`}>この調査で完成した評価版を見る →</Link>}
        <Link className="w-fit text-xs underline" to={`/securities/${target.ticker}`}>最新の銘柄情報を見る →</Link>
      </div>)}
      {research.proposal_id && <Link className="w-fit text-sm underline" to={`/stocks/investment-proposals/${research.proposal_id}`}>この調査の保存提案・条件・未完了理由を見る →</Link>}
      {research.source_run_revision_id && <p className="text-xs text-muted-foreground">保存した分析基準日 {research.source_analysis_as_of} · Run: {research.source_run_revision_id}</p>}
      <details className="text-xs text-muted-foreground"><summary className="cursor-pointer">今回の記録・参照</summary><div className="mt-2 grid gap-2 break-words"><p>{research.request}</p><p>{String(operation.payload.result ?? operation.payload.checkpoint)}</p>{operation.payload.next != null && <p>次の確認: {String(operation.payload.next)}</p>}<p className="break-all">Operation: {operation.operation_id}</p>{research.targets.map(target => <p className="break-all" key={`${target.purpose}-${target.ticker}`}>{target.ticker} · {target.assessment_ref?.assessment_id ?? ''}{target.references.length > 0 && ` · ${target.references.join(' · ')}`}</p>)}</div></details>
    </article>
  })
  return <SectionCard padded title="調査と提案の進行" description="未完了は投資判断としての見送りと区別します。調査終了は人間の採否・注文・約定を意味しません。">
    {content.length === 0 ? <p className="text-sm text-muted-foreground">調査・運用の進行記録はまだありません。</p> : <div className="grid gap-4">{content.slice(0, 3)}{content.length > 3 && <details><summary className="cursor-pointer text-sm">以前の調査・運用記録（{content.length - 3}件）</summary><div className="mt-4 grid gap-4">{content.slice(3)}</div></details>}</div>}
  </SectionCard>
}
