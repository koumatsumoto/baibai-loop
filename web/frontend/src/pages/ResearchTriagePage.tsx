import { useEffect, useState } from 'react'
import { Link } from 'react-router'

import { fetchJson } from '../api/client'
import type { ScreeningView } from '../api/types'
import { AsOfBadge } from '../components/AsOfBadge'
import { UpdatedAtBadge } from '../components/UpdatedAtBadge'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageShell } from '../components/PageShell'
import { PageState } from '../components/PageState'
import { Badge } from '../components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../components/ui/table'

export function ResearchTriagePage() {
  const [data, setData] = useState<ScreeningView | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    fetchJson<ScreeningView>('/api/screening/latest').then(setData).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : 'Research Triage を読み込めませんでした'))
  }, [])
  if (error) return <PageState message={error} title="Research Triage read error" />
  if (!data) return <LoadingPage label="Research Triage を読み込んでいます" />
  const triage = data.research_triages[0]
  if (!triage) return <PageState message="Research Triage はまだ publish されていません" title="Research Triage" />
  const reviewSet = data.review_sets.find((item) => item.review_set_id === triage.review_set_id)
  const names = new Map(reviewSet?.entries.map((item) => [item.ticker, item.name]) ?? [])
  const entries = [...triage.entries].sort((left, right) =>
    (left.priority ?? Number.MAX_SAFE_INTEGER) - (right.priority ?? Number.MAX_SAFE_INTEGER)
    || left.ticker.localeCompare(right.ticker))
  return (
    <PageShell lead="AIが4 Approachをprimary、機械E[r]をsecondary return priorとして全候補を比較し、Research / Skipと調査優先度を記録した面です。最終的なbuy判断ではありません。" meta={<div className="flex flex-wrap gap-4 font-mono text-xs text-muted-foreground"><UpdatedAtBadge value={triage.published_at} /><AsOfBadge value={triage.as_of} /><Badge variant="secondary">{triage.research_triage_id}</Badge></div>} title="Research Triage">
      <Table><TableHeader><TableRow><TableHead>優先度</TableHead><TableHead>銘柄</TableHead><TableHead>判断</TableHead><TableHead>理由</TableHead><TableHead>Research Question</TableHead><TableHead>Key Risk</TableHead></TableRow></TableHeader><TableBody>{entries.map((entry) => <TableRow key={entry.ticker}><TableCell>{entry.priority ?? '—'}</TableCell><TableCell><Link className="font-mono font-semibold hover:underline" to={`/securities/${entry.ticker}`}>{entry.ticker}</Link><span className="ml-2 text-muted-foreground">{names.get(entry.ticker)}</span></TableCell><TableCell><Badge variant={entry.decision === 'research' ? 'default' : 'secondary'}>{entry.decision}</Badge></TableCell><TableCell>{entry.rationale}</TableCell><TableCell>{entry.research_question ?? '—'}</TableCell><TableCell>{entry.key_risk ?? '—'}</TableCell></TableRow>)}</TableBody></Table>
    </PageShell>
  )
}
