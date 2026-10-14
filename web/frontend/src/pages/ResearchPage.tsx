import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { fetchJson } from '../api/client'
import type { InvestmentProposalSummaryView, InvestmentProposalsView, DashboardView, OperationsView } from '../api/types'
import { AsOfBadge } from '../components/AsOfBadge'
import { UpdatedAtBadge } from '../components/UpdatedAtBadge'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageShell } from '../components/PageShell'
import { ResearchNav } from '../components/ResearchNav'
import { SectionCard } from '../components/SectionCard'
import { Badge } from '../components/ui/badge'
import { ACTION_LABELS } from '../lib/research'
import { ResearchOperationCard } from './research/ResearchOperationCard'

export function InvestmentProposals({ proposals, error }: { proposals: InvestmentProposalSummaryView[]; error?: string | null }) {
  const cards = [...proposals].sort((a, b) => Date.parse(b.published_at) - Date.parse(a.published_at)).map(item => <Link className="grid gap-2 rounded-lg border p-4 transition-colors hover:bg-muted/40" key={item.proposal_id} to={`/stocks/investment-proposals/${item.proposal_id}`}>
    <div className="flex flex-wrap items-center gap-2">{item.actions.map(action => <Badge key={action} variant={action === 'buy' ? 'default' : 'secondary'}>{ACTION_LABELS[action] ?? action}</Badge>)}<AsOfBadge value={item.as_of} /><UpdatedAtBadge value={item.published_at} /></div>
    <strong className="font-medium">{item.headline}</strong>
    <p className="text-xs text-muted-foreground">対象 {item.item_count}件 · 保存した行動・条件・根拠を見る →</p>
  </Link>)
  return <SectionCard title="投資提案・見送り・保留" description="保存した当時の提案です。人間の採否・注文・約定は別工程です。" padded>
    {error ? <p role="alert" className="text-sm text-destructive">提案一覧を読み込めませんでした。提案がないことを意味しません。</p> : cards.length === 0 ? <p className="text-sm text-muted-foreground">表示できる投資提案はありません。調査の進行・未完了は下で確認できます。</p> : <div className="grid gap-3">{cards.slice(0, 3)}{cards.length > 3 && <details><summary className="cursor-pointer text-sm">過去の提案（{cards.length - 3}件）</summary><div className="mt-3 grid gap-3">{cards.slice(3)}</div></details>}</div>}
  </SectionCard>
}

export function ResearchPage() {
  const [data, setData] = useState<InvestmentProposalsView | null>(null)
  const [names, setNames] = useState<ReadonlyMap<string, string | null>>(new Map())
  const [operations, setOperations] = useState<OperationsView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [operationError, setOperationError] = useState<string | null>(null)
  useEffect(() => {
    fetchJson<InvestmentProposalsView>('/api/investment-proposals').then(setData).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : '提案を読み込めませんでした'))
    fetchJson<DashboardView>('/api/dashboard').then(view => setNames(new Map(view.holdings.map(item => [item.ticker, item.company_name])))).catch(() => {})
    fetchJson<OperationsView>('/api/operations').then(setOperations).catch((reason: unknown) => setOperationError(reason instanceof Error ? reason.message : '調査進捗を読み込めませんでした'))
  }, [])
  if (!data && !error) return <LoadingPage label="提案を読み込んでいます" />
  return <PageShell above={<ResearchNav />} title="調査・提案" lead="完成した提案と、調査の進行を確認します。開始・再開は明示的に依頼し、採否と注文は人間が決めます。">
    <InvestmentProposals proposals={data?.proposals ?? []} error={error} />
    {operationError ? <p role="alert" className="text-sm text-destructive">調査進捗を読み込めませんでした。記録がないことを意味しません。</p> : operations ? <ResearchOperationCard operations={operations.operations} names={names} /> : <p className="text-sm text-muted-foreground">調査進捗を読み込んでいます。</p>}
    <div className="flex flex-wrap gap-4 text-sm"><Link className="underline" to="/stocks/explore">調査候補を探す</Link><Link className="underline" to="/market-context">市場・業種環境の根拠を見る</Link></div>
  </PageShell>
}
