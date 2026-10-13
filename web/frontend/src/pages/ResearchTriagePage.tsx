import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { fetchJson } from '../api/client'
import type { ResearchTriageView, ScreeningView } from '../api/types'
import { AsOfBadge } from '../components/AsOfBadge'
import { UpdatedAtBadge } from '../components/UpdatedAtBadge'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageShell } from '../components/PageShell'
import { PageState } from '../components/PageState'
import { ResearchNav } from '../components/ResearchNav'
import { Badge } from '../components/ui/badge'

export function ResearchTriageEntries({ triage, names }: { triage: ResearchTriageView; names: ReadonlyMap<string, string | null> }) {
  const entries = [...triage.entries].sort((a, b) => (a.priority ?? Number.MAX_SAFE_INTEGER) - (b.priority ?? Number.MAX_SAFE_INTEGER) || a.ticker.localeCompare(b.ticker))
  return <div className="grid gap-3">{entries.map(entry => <article className="grid gap-3 rounded-lg border bg-card p-4" key={entry.ticker}>
    <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="font-medium">{entry.review_set_entry_snapshot?.name ?? names.get(entry.ticker) ?? entry.ticker}<span className="ml-2 font-mono text-xs text-muted-foreground">{entry.ticker}</span></h2><div className="flex flex-wrap gap-2"><Badge variant={entry.decision === 'research' ? 'default' : 'secondary'}>{entry.decision === 'research' ? '調査対象' : entry.decision === 'skip' ? '調査見送り' : entry.decision}</Badge>{entry.priority !== null && <Badge variant="outline">調査優先 {entry.priority}</Badge>}</div></div>
    <p className="text-sm">{entry.rationale}</p>
    <div className="grid gap-3 text-sm sm:grid-cols-2">{entry.research_question && <div><p className="text-xs font-medium text-muted-foreground">調査で確かめたいこと</p><p className="mt-1">{entry.research_question}</p></div>}{entry.key_risk && <div><p className="text-xs font-medium text-muted-foreground">主なリスク</p><p className="mt-1">{entry.key_risk}</p></div>}</div>
    <Link className="w-fit text-xs underline" to={`/securities/${entry.ticker}`}>最新の銘柄情報を見る →</Link>
  </article>)}</div>
}
export function ResearchTriagePage() {
  const [data, setData] = useState<ScreeningView | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    fetchJson<ScreeningView>('/api/screening/latest').then(setData).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : '調査判断を読み込めませんでした'))
  }, [])
  if (error) return <PageState message={error} title="調査判断" />
  if (!data) return <LoadingPage label="調査判断を読み込んでいます" />
  const triage = data.research_triages[0]
  if (!triage) return <PageState message="表示できる調査判断はまだありません" title="調査優先度・見送りの根拠" />
  const reviewSet = data.review_sets.find(item => item.review_set_id === triage.review_set_id)
  const names = new Map(reviewSet?.entries.map(item => [item.ticker, item.name]) ?? [])
  return <PageShell above={<ResearchNav />} title="調査優先度・見送りの根拠" lead="AIが候補を比較した記録です。調査の優先度は、購入・配分の提案とは別です。" meta={<div className="flex flex-wrap gap-3"><UpdatedAtBadge value={triage.published_at} /><AsOfBadge value={triage.as_of} /></div>}>
    <ResearchTriageEntries triage={triage} names={names} />
    <details className="text-xs text-muted-foreground"><summary className="cursor-pointer">判断の参照記録</summary><p className="mt-2 break-all">Research Triage: {triage.research_triage_id} · Review Set: {triage.review_set_id} · Run: {triage.run_revision_id}</p></details>
  </PageShell>
}
