import { useEffect, useState } from 'react'
import { ArrowLeft, ArrowUpRight, Clock3 } from 'lucide-react'
import { Link, useSearchParams } from 'react-router'

import { fetchJson } from '../api/client'
import type { ProposalHistoryView, SavedProposalView } from '../api/types'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageShell } from '../components/PageShell'
import { PageState } from '../components/PageState'
import { SavedBuySession } from '../components/SavedBuySession'
import { SectionCard } from '../components/SectionCard'
import { Badge } from '../components/ui/badge'
import { formatJstDateTime } from '../lib/format'
import { proposalActionLabel, groupProposalReports, proposalReportHref, reportListSummary } from '../lib/proposal-history'
import { ProposalReport } from './ProposalPage'

function BackToReports() {
  return <Link className="inline-flex w-fit items-center gap-2 rounded text-sm text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring" to="/proposal"><ArrowLeft aria-hidden="true" className="size-4" />日本株のレポート一覧</Link>
}

// An omitted collection cannot establish that there are no saved corrections or reports.
function hasProposalRecordCollections({ corrections, human_reports }: SavedProposalView): boolean {
  return Array.isArray(corrections) && Array.isArray(human_reports)
}

type HistoryEnvelope = Omit<ProposalHistoryView, 'proposals'> & { proposals: unknown[] }

// Only a consistent explicit empty response establishes the absence of saved reports.
function isHistoryEnvelope(data: unknown): data is HistoryEnvelope {
  if (data === null || typeof data !== 'object') return false
  const { status, error, generated_at, proposals, unavailable_proposal_ids } = data as Partial<HistoryEnvelope>
  if (!Array.isArray(proposals) || !Array.isArray(unavailable_proposal_ids)
      || typeof generated_at !== 'string' || !Number.isFinite(Date.parse(generated_at))) return false
  const hasReports = proposals.length > 0
  const hasMissing = unavailable_proposal_ids.length > 0
  switch (status) {
    case 'ready': return hasReports && !hasMissing && error === null
    case 'empty': return !hasReports && !hasMissing && error === null
    case 'partial': return hasReports && hasMissing && typeof error === 'string'
    case 'unavailable': return !hasReports && (typeof error === 'string' || error === null)
    default: return false
  }
}

function savedProposalId(row: unknown): string | undefined {
  return row !== null && typeof row === 'object' && 'proposal_id' in row
    && typeof row.proposal_id === 'string' ? row.proposal_id : undefined
}

// Check the fields this page traverses before choosing a detail or grouping rows.
function hasDisplayFields(row: unknown): row is SavedProposalView {
  if (savedProposalId(row) === undefined) return false
  const { published_at, items } = row as Partial<SavedProposalView>
  return typeof published_at === 'string' && Number.isFinite(Date.parse(published_at))
    && Array.isArray(items) && items.length > 0
    && items.every(item => item !== null && typeof item === 'object' && typeof item.ticker === 'string')
}

function UnreadableHistory({ message, unavailableCount = 0 }: { message: string; unavailableCount?: number }) {
  return <PageShell title="日本株の売買提案を読み取れません" above={<BackToReports />} width="reading">
    <SectionCard padded title="保存済みレポートを読み取れません">
      <p>{message}</p>
      {unavailableCount > 0 && <p className="mt-2">読取不能の提案：{unavailableCount}件。保存済み提案がないという意味ではありません。</p>}
    </SectionCard>
  </PageShell>
}

export function ProposalHistory({ data, proposalId = null, ticker = null }: {
  data: unknown
  proposalId?: string | null
  ticker?: string | null
}) {
  if (data === undefined) return <LoadingPage label="日本株の提案履歴を読み込んでいます" />
  if (!isHistoryEnvelope(data)) return <UnreadableHistory message="配信された履歴の形式が不正です。" />
  if (data.status === 'unavailable') {
    return <UnreadableHistory message={data.error ?? '履歴を読み取れませんでした。'} unavailableCount={data.unavailable_proposal_ids.length} />
  }
  if (proposalId !== null || ticker !== null) {
    const match = proposalId === null ? undefined : data.proposals.find(row => savedProposalId(row) === proposalId)
    const proposal = hasDisplayFields(match) ? match : undefined
    const missingRecords = proposal !== undefined && !hasProposalRecordCollections(proposal)
    const item = missingRecords ? undefined : proposal?.items.find(item => item.ticker === ticker)
    if (proposal === undefined || item === undefined) {
      const reason = match !== undefined && proposal === undefined
        ? '配信データの形式が不正なため、この保存レポートは表示していません。'
        : missingRecords
          ? '訂正・人間の報告を確認できないため、この保存レポートは表示していません。'
          : proposalId !== null && data.unavailable_proposal_ids.includes(proposalId)
            ? 'この提案は読取不能です。別の時点の提案では代用していません。'
            : '指定した提案・銘柄の組合せは、この表示データにありません。一覧から選び直してください。'
      return <PageShell title="日本株のレポートを表示できません" above={<BackToReports />} width="reading"><SectionCard padded title="指定された保存レポートを確認できません">
        <p>{reason}</p>
      </SectionCard></PageShell>
    }
    return <ProposalReport key={`${proposalId}:${ticker}`} proposal={proposal} item={item} generatedAt={data.generated_at} above={<BackToReports />} />
  }
  const readable = data.proposals.filter((row): row is SavedProposalView =>
    hasDisplayFields(row) && hasProposalRecordCollections(row),
  )
  const malformedCount = data.proposals.length - readable.length
  const groups = groupProposalReports(readable)
  const reportCount = groups.reduce((total, group) => total + group.reports.length, 0)
  const countLabel = malformedCount > 0 ? `表示できるレポート：${reportCount}件` : `${reportCount}件のレポート`
  return <PageShell title="日本株の売買提案" lead="円建ての保存提案です。銘柄を選ぶと、判断と根拠を読めます。" meta={<span className="text-sm text-muted-foreground">{countLabel}</span>} width="reading">
    <div className="grid gap-2 text-xs leading-6 text-muted-foreground">
      <p>発行日時（JST）の新しい順。午前は0〜11時、午後は12〜23時です。提案基準日や再生成日時とは異なります。</p>
      <p>同じ銘柄でも過去のレポートは別に残しています。現在の売買推奨・注文可否ではありません。</p>
      {data.unavailable_proposal_ids.length > 0 && <p role="status" className="rounded-lg border border-warning/30 bg-warning-surface px-4 py-3 text-sm text-warning-ink">読取不能の提案が{data.unavailable_proposal_ids.length}件あります。その提案に含まれる銘柄は一覧に表示できていません。</p>}
      {malformedCount > 0 && <p role="status" className="rounded-lg border border-warning/30 bg-warning-surface px-4 py-3 text-sm text-warning-ink">配信データの形式が不正な提案が{malformedCount}件あります。該当するレポートは表示していません。</p>}
    </div>
    {data.status === 'empty' && <SectionCard padded title="日本株の保存済みレポートはありません"><p className="text-sm text-muted-foreground">表示データ生成時点で、正式に保存された提案はありません。</p></SectionCard>}
    {groups.map(group => <section className="grid min-w-0 gap-3" key={group.key} aria-labelledby={`reports-${group.key}`}>
      <h2 id={`reports-${group.key}`} className="flex items-baseline gap-3 text-lg font-semibold">{group.label}<span className="text-xs font-normal text-muted-foreground">{group.year}年 · JST</span></h2>
      <div className="grid min-w-0 gap-3 md:grid-cols-2">
        {group.reports.map(({ proposal, item, publicationTime, corrected }) => <Link key={`${proposal.proposal_id}:${item.ticker}`} to={proposalReportHref(proposal.proposal_id, item.ticker)} className="group grid min-w-0 content-start gap-3 rounded-xl border bg-card p-4 transition-colors hover:border-border-strong hover:bg-primary-soft focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring sm:p-5">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0"><h3 className="break-words text-lg font-semibold">{item.company_name ?? '会社名未記録'}</h3><p className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground"><span className="font-mono">{item.ticker}</span><span>{item.sector ?? '業種未記録'}</span></p></div>
            <ArrowUpRight aria-hidden="true" className="mt-1 size-4 shrink-0 text-muted-foreground group-hover:text-foreground" />
          </div>
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <Badge variant="secondary">{proposalActionLabel(item)}</Badge>
            {corrected && <Badge variant="outline">訂正で候補から外れた提案</Badge>}
            <SavedBuySession item={item} />
          </div>
          <p className="break-words text-sm leading-6">{reportListSummary({ proposal, item })}</p>
          <div className="flex flex-wrap items-center justify-between gap-2 border-t pt-3 text-xs text-muted-foreground"><span className="inline-flex items-center gap-1.5"><Clock3 aria-hidden="true" className="size-3.5" /><time dateTime={proposal.published_at}>{publicationTime} 発行</time></span><span>提案基準日 {proposal.as_of}</span></div>
        </Link>)}
      </div>
    </section>)}
    <p className="text-xs text-muted-foreground">表示データ生成 <time dateTime={data.generated_at}>{formatJstDateTime(data.generated_at)} JST</time></p>
  </PageShell>
}

export function ProposalPage() {
  const [data, setData] = useState<unknown>(undefined)
  const [error, setError] = useState<string | null>(null)
  const [params] = useSearchParams()
  const proposalId = params.get('proposal')
  const ticker = params.get('ticker')
  useEffect(() => {
    const controller = new AbortController()
    fetchJson<unknown>('/api/proposal/history', { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setData(value) })
      .catch((reason: unknown) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '売買提案を読み込めませんでした。') })
    return () => controller.abort()
  }, [])
  // A report chosen near the bottom of a long list must open at its summary.
  useEffect(() => { window.scrollTo(0, 0) }, [proposalId, ticker])
  if (error !== null) return <PageState message={error} title="日本株の提案履歴の通信エラー" />
  return <ProposalHistory data={data} proposalId={proposalId} ticker={ticker} />
}
