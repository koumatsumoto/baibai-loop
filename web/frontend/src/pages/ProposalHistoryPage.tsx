import { useEffect, useState } from 'react'
import { ArrowLeft, ArrowUpRight, Clock3 } from 'lucide-react'
import { Link, useSearchParams } from 'react-router'

import { fetchJson } from '../api/client'
import type { ProposalHistoryView } from '../api/types'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageShell } from '../components/PageShell'
import { PageState } from '../components/PageState'
import { SectionCard } from '../components/SectionCard'
import { Badge } from '../components/ui/badge'
import { formatJstDateTime } from '../lib/format'
import { ACTION_LABEL, groupProposalReports, isCorrectedProposal, proposalReportHref, publicationPeriod } from '../lib/proposal-history'
import { ProposalReport } from './ProposalPage'

function BackToReports() {
  return <Link className="inline-flex w-fit items-center gap-2 rounded text-sm text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring" to="/proposal"><ArrowLeft aria-hidden="true" className="size-4" />レポート一覧</Link>
}

export function ProposalHistory({ data, proposalId = null, ticker = null }: {
  data: ProposalHistoryView | null
  proposalId?: string | null
  ticker?: string | null
}) {
  if (data === null) return <LoadingPage label="売買提案の履歴を読み込んでいます" />
  if (data.status === 'unavailable') return <PageShell title="売買提案の読取不能" above={<BackToReports />} width="reading">
    <SectionCard padded title="保存済みレポートを読み取れません">
      <p>{data.error ?? '履歴を読み取れませんでした。'}</p>
      {data.unavailable_proposal_ids.length > 0 && <p className="mt-2">読取不能の提案：{data.unavailable_proposal_ids.length}件。保存済み提案がないという意味ではありません。</p>}
    </SectionCard>
  </PageShell>
  if (proposalId !== null || ticker !== null) {
    const proposal = data.proposals.find(row => row.proposal_id === proposalId)
    if (proposal === undefined || !proposal.items.some(item => item.ticker === ticker)) {
      return <PageShell title="レポートを表示できません" above={<BackToReports />} width="reading"><SectionCard padded title="指定された保存レポートを確認できません">
        <p>{proposalId !== null && data.unavailable_proposal_ids.includes(proposalId) ? 'この提案は読取不能です。別の時点の提案では代用していません。' : '指定した提案・銘柄の組合せは、この表示データにありません。一覧から選び直してください。'}</p>
      </SectionCard></PageShell>
    }
    return <ProposalReport key={`${proposalId}:${ticker}`} data={{ generated_at: data.generated_at, status: 'ready', error: null, proposal }} ticker={ticker ?? undefined} above={<BackToReports />} />
  }
  const groups = groupProposalReports(data.proposals)
  const reportCount = groups.reduce((total, group) => total + group.reports.length, 0)
  return <PageShell title="売買提案" lead="銘柄を選んで、保存時点の判断と根拠を読む。" meta={<span className="text-sm text-muted-foreground">{reportCount}件のレポート</span>} width="reading">
    <div className="grid gap-2 text-xs leading-6 text-muted-foreground">
      <p>発行日時（JST）の新しい順。午前は0〜11時、午後は12〜23時です。提案基準日や再生成日時とは異なります。</p>
      <p>同じ銘柄でも過去のレポートは別に残しています。現在の売買推奨・注文可否ではありません。</p>
      {data.unavailable_proposal_ids.length > 0 && <p role="status" className="rounded-lg border border-amber-500/30 bg-amber-500/5 px-4 py-3 text-sm text-foreground">読取不能の提案が{data.unavailable_proposal_ids.length}件あります。その提案に含まれる銘柄は一覧に表示できていません。</p>}
    </div>
    {groups.length === 0 && <SectionCard padded title="保存済みレポートはありません"><p className="text-sm text-muted-foreground">表示データ生成時点で、正式に保存された提案はありません。</p></SectionCard>}
    {groups.map(group => <section className="grid min-w-0 gap-3" key={group.key} aria-labelledby={`reports-${group.key}`}>
      <h2 id={`reports-${group.key}`} className="flex items-baseline gap-3 text-lg font-semibold">{group.label}<span className="text-xs font-normal text-muted-foreground">{group.year}年 · JST</span></h2>
      <div className="grid min-w-0 gap-3 md:grid-cols-2">
        {group.reports.map(({ proposal, item }) => <Link key={`${proposal.proposal_id}:${item.ticker}`} to={proposalReportHref(proposal.proposal_id, item.ticker)} className="group grid min-w-0 content-start gap-4 rounded-xl border bg-card p-5 transition-colors hover:border-primary/40 hover:bg-muted/20 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring sm:p-6">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0"><h3 className="break-words text-lg font-semibold">{item.company_name ?? '会社名未記録'}</h3><p className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground"><span className="font-mono">{item.ticker}</span><span>{item.sector ?? '業種未記録'}</span></p></div>
            <ArrowUpRight aria-hidden="true" className="mt-1 size-4 shrink-0 text-muted-foreground group-hover:text-foreground" />
          </div>
          <div className="flex flex-wrap items-center gap-2"><Badge variant="secondary">{ACTION_LABEL[item.action]}</Badge>{isCorrectedProposal(proposal) && <Badge variant="outline">訂正済みの旧提案</Badge>}</div>
          <p className="whitespace-pre-wrap break-words text-sm leading-7">{item.rationale}</p>
          <div className="flex flex-wrap items-center justify-between gap-2 border-t pt-3 text-xs text-muted-foreground"><span className="inline-flex items-center gap-1.5"><Clock3 aria-hidden="true" className="size-3.5" /><time dateTime={proposal.published_at}>{publicationPeriod(proposal.published_at).time} 発行</time></span><span>提案基準日 {proposal.as_of}</span></div>
        </Link>)}
      </div>
    </section>)}
    <p className="text-xs text-muted-foreground">表示データ生成 <time dateTime={data.generated_at}>{formatJstDateTime(data.generated_at)} JST</time></p>
  </PageShell>
}

export function ProposalPage() {
  const [data, setData] = useState<ProposalHistoryView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [params] = useSearchParams()
  useEffect(() => {
    const controller = new AbortController()
    fetchJson<ProposalHistoryView>('/api/proposal/history', { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setData(value) })
      .catch((reason: unknown) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '売買提案を読み込めませんでした。') })
    return () => controller.abort()
  }, [])
  if (error !== null) return <PageState message={error} title="売買提案の通信エラー" />
  return <ProposalHistory data={data} proposalId={params.get('proposal')} ticker={params.get('ticker')} />
}
