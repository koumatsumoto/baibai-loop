import { useEffect, useState } from 'react'
import { ExternalLink } from 'lucide-react'

import { fetchJson } from '../api/client'
import type {
  RunBatchView,
  RunErrorView,
  RunOutcome,
  RunPublishState,
  WorkflowRunSummaryView,
} from '../api/run-summary'
import type { SystemStoreName, SystemView } from '../api/types'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageShell } from '../components/PageShell'
import { SectionCard } from '../components/SectionCard'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Card, CardContent } from '../components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../components/ui/table'
import { EMPTY, formatJstDate, formatJstDateTime } from '../lib/format'
import { ACTIONS_URL } from '../lib/nav'
import { cn } from '../lib/utils'

const STORE_LABEL: Record<SystemStoreName, { name: string; rows: string }> = {
  market: { name: 'market — 価格・calendar', rows: '日次バー' },
  runs: { name: 'runs — screening run', rows: 'run' },
  macro: { name: 'macro — 指標系列', rows: '観測' },
  baibai: { name: 'baibai — 判断記録', rows: EMPTY },
}

const OUTCOME_LABEL: Record<RunOutcome, string> = {
  succeeded: '正常終了',
  skipped_non_business_day: '非営業日 skip',
  published_with_deferred_failure: '公開済み・繰延べ失敗あり',
  failed: '失敗',
  cancelled: '中断（timeout / cancel）',
}

const PUBLISH_LABEL: Record<RunPublishState, string> = {
  not_generated: '未生成',
  generated: '生成のみ',
  upload_failed: 'upload 失敗',
  published: '公開済み',
}

const BATCH_KIND_LABEL = { daily: '日次バッチ', manual: '手動 materialize' } as const

function storeLabel(store: SystemStoreName) {
  // An exporter that learns a fifth store must not blank the page on a UI that
  // predates it.
  return STORE_LABEL[store] ?? { name: store, rows: EMPTY }
}

function outcomeTone(outcome: RunOutcome): string {
  if (outcome === 'succeeded') return 'border-positive/50 text-positive'
  if (outcome === 'skipped_non_business_day') return 'text-muted-foreground'
  if (outcome === 'published_with_deferred_failure') return 'border-warning/50 text-warning'
  return 'border-destructive/50 text-destructive'
}

// Elapsed time is shown, not judged. The batch runs on TSE business days, so any
// fixed threshold cries every Monday and through every holiday week; the reader
// applies their own, the way every other freshness value here is read.
export function elapsedLabel(iso: string, now: number = Date.now()): string {
  const parsed = Date.parse(iso)
  if (Number.isNaN(parsed)) return ''
  const hours = (now - parsed) / 3_600_000
  if (hours < 0) return ''
  if (hours < 1) return '1 時間以内'
  if (hours < 24) return `${Math.floor(hours)} 時間前`
  return `${Math.floor(hours / 24)} 日前`
}

function formatBytes(value: number | null): string {
  if (typeof value !== 'number') return EMPTY
  const mib = value / 1024 / 1024
  return mib >= 1024 ? `${(mib / 1024).toFixed(1)} GiB` : `${mib.toFixed(1)} MiB`
}

function formatCount(value: number | null): string {
  return typeof value === 'number' ? value.toLocaleString('ja-JP') : EMPTY
}

// The serving object is cast, not parsed, and a run summary from a newer exporter
// would throw mid-render. Check the shape this card actually dereferences.
function isRenderableRun(run: WorkflowRunSummaryView): boolean {
  return (
    run.schema_version === 1 &&
    typeof run.duration_seconds === 'number' &&
    typeof run.finished_at === 'string' &&
    typeof run.run_url === 'string' &&
    run.run_url.startsWith('https://') &&
    Array.isArray(run.workflow_errors) &&
    typeof run.execution === 'object' &&
    run.execution !== null
  )
}

function DetailRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid gap-0.5">
      <dt className="text-[10px] font-semibold tracking-wide text-muted-foreground">{label}</dt>
      <dd className="font-mono text-sm tabular-nums">{children}</dd>
    </div>
  )
}

function ErrorList({ errors }: { errors: RunErrorView[] }) {
  if (errors.length === 0) return null
  return (
    <ul className="grid gap-1.5">
      {errors.map((error, index) => (
        <li className="flex flex-wrap items-baseline gap-x-2 text-xs" key={`${error.stage}-${error.code}-${index}`}>
          <Badge className={cn('font-mono text-[10px]', error.impact === 'failed' ? 'border-destructive/50 text-destructive' : 'border-warning/50 text-warning')} variant="outline">
            {error.stage}
          </Badge>
          <span className="text-muted-foreground">{error.message}</span>
        </li>
      ))}
    </ul>
  )
}

function BatchRow({ batch }: { batch: RunBatchView }) {
  const metrics = Object.entries(batch.metrics ?? {})
  return (
    <div className="grid gap-1.5 border-b py-3 last:border-0 last:pb-0">
      <div className="flex flex-wrap items-center gap-2">
        <strong className="font-mono text-sm">{batch.batch_name}</strong>
        <Badge variant={batch.status === 'ok' ? 'secondary' : 'outline'}>{batch.status}</Badge>
        {typeof batch.duration_seconds === 'number' && <span className="text-xs text-muted-foreground">{batch.duration_seconds.toFixed(1)}s</span>}
        <span className="text-xs text-muted-foreground">{(batch.datasets ?? []).join(' / ')}</span>
      </div>
      {metrics.length > 0 && (
        <div className="flex flex-wrap gap-x-4 gap-y-1 font-mono text-xs text-muted-foreground">
          {metrics.map(([key, value]) => (
            <span key={key}>{key}=<span className="text-foreground">{String(value)}</span></span>
          ))}
        </div>
      )}
      <ErrorList errors={batch.errors ?? []} />
    </div>
  )
}

function NoRunCard({ reason }: { reason: string }) {
  return (
    <SectionCard description={reason} padded title="直近の日次バッチ">
      <>
        <Button asChild size="sm" variant="outline">
          <a href={ACTIONS_URL} rel="noreferrer noopener" target="_blank"><ExternalLink />GitHub Actions で確認</a>
        </Button>
      </>
    </SectionCard>
  )
}

function LatestRunCard({ run }: { run: WorkflowRunSummaryView | null }) {
  if (run === null) {
    return <NoRunCard reason="run の記録がまだありません。cloud で 1 回走ると表示されます。" />
  }
  if (!isRenderableRun(run)) {
    return <NoRunCard reason="run の記録がこの UI の想定と一致しません。deploy と materialize の世代を確認してください。" />
  }

  const execution = run.execution
  const batches = execution.kind === 'available' ? (execution.summary?.batches ?? []) : []
  const elapsed = elapsedLabel(run.finished_at)

  return (
    <SectionCard
      description={`${run.workflow} · ${run.trigger} · attempt ${run.run_attempt}`}
      meta={(
        <Badge className={cn('text-xs', outcomeTone(run.overall_outcome))} variant="outline">
          {OUTCOME_LABEL[run.overall_outcome] ?? run.overall_outcome}
        </Badge>
      )}
      padded
      title="直近の日次バッチ"
    >
      <div className="grid gap-4">
        <dl className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-5">
          <DetailRow label="run 終了">
            {formatJstDateTime(run.finished_at)}
            {elapsed !== '' && <span className="ml-2 text-xs text-muted-foreground">{elapsed}</span>}
          </DetailRow>
          <DetailRow label="対象日">{run.asof ?? EMPTY}</DetailRow>
          <DetailRow label="所要">{run.duration_seconds.toFixed(0)}s</DetailRow>
          <DetailRow label="公開状態">{PUBLISH_LABEL[run.publish_state] ?? run.publish_state}</DetailRow>
          <DetailRow label="通知">{run.delivery?.status ?? EMPTY}</DetailRow>
        </dl>

        {execution.kind === 'not_started' && (
          <p className="text-sm text-muted-foreground">
            バッチ本体に到達していません（{execution.stage ?? '不明'} で停止）。
          </p>
        )}
        {execution.kind === 'unavailable' && execution.error && (
          <ErrorList errors={[execution.error]} />
        )}
        {batches.length > 0 && <div className="grid">{batches.map((batch) => <BatchRow batch={batch} key={batch.batch_name} />)}</div>}
        {run.workflow_errors.length > 0 && (
          <div className="grid gap-1.5">
            <span className="text-xs font-semibold text-muted-foreground">workflow error</span>
            <ErrorList errors={run.workflow_errors} />
          </div>
        )}

        <Button asChild className="w-fit" size="sm" variant="outline">
          <a href={run.run_url} rel="noreferrer noopener" target="_blank"><ExternalLink />この run の log を開く</a>
        </Button>
      </div>
    </SectionCard>
  )
}

function StoresCard({ data }: { data: SystemView }) {
  return (
    <SectionCard description="日付は store が持つ最新データ、件数は代表テーブルの行数" title="store の鮮度と規模">
      <div className="overflow-x-auto">
        <Table className="min-w-[560px] text-sm">
          <TableHeader className="bg-muted/60">
            <TableRow className="hover:bg-transparent">
              <TableHead className="pl-5 sm:pl-6">store</TableHead>
              <TableHead>最新データ</TableHead>
              <TableHead className="text-right">件数</TableHead>
              <TableHead className="pr-5 text-right sm:pr-6">サイズ</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {data.stores.map((store) => (
              <TableRow key={store.store}>
                <TableCell className="pl-5 font-medium sm:pl-6">{storeLabel(store.store).name}</TableCell>
                <TableCell className="font-mono tabular-nums">
                  {!store.exists
                    ? <span className="text-muted-foreground">store なし</span>
                    : store.latest_date !== null
                      ? formatJstDate(store.latest_date)
                      : store.updated_at !== null
                        ? formatJstDateTime(store.updated_at)
                        : <span className="text-muted-foreground">読めません</span>}
                </TableCell>
                <TableCell className="text-right font-mono tabular-nums">
                  {formatCount(store.row_count)}
                  {typeof store.row_count === 'number' && <span className="ml-1 text-xs text-muted-foreground">{storeLabel(store.store).rows}</span>}
                </TableCell>
                <TableCell className="pr-5 text-right font-mono tabular-nums sm:pr-6">{formatBytes(store.size_bytes)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </SectionCard>
  )
}

function ProvidersCard({ data }: { data: SystemView }) {
  const failing = data.failing_providers
  const neverAttempted = data.never_attempted_series ?? []
  const unhealthy = failing.length + neverAttempted.length
  return (
    <SectionCard
      description="直近の取得が失敗したままの系列。ローカル実行の記録も同じ store に入る"
      meta={<Badge variant={unhealthy === 0 ? 'secondary' : 'destructive'}>{unhealthy} / {data.provider_series_total} 系列</Badge>}
      title="provider 取得の健全性"
    >
      {unhealthy === 0 ? (
        <p className="py-8 text-center text-sm text-muted-foreground">
          全 {data.provider_series_total} 系列の直近取得が成功しています。
        </p>
      ) : (
        <div className="overflow-x-auto">
          {failing.length > 0 && (
            <Table className="min-w-[640px] text-sm">
              <TableHeader className="bg-muted/60">
                <TableRow className="hover:bg-transparent">
                  <TableHead className="pl-5 sm:pl-6">系列</TableHead>
                  <TableHead className="text-right">連続失敗</TableHead>
                  <TableHead>失敗開始</TableHead>
                  <TableHead className="pr-5 sm:pr-6">直近の error</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {failing.map((provider) => (
                  <TableRow key={provider.series_id}>
                    <TableCell className="pl-5 sm:pl-6">
                      <span className="font-mono text-xs">{provider.series_id}</span>
                      <span className="mt-0.5 block max-w-64 truncate text-xs text-muted-foreground">{provider.name}</span>
                    </TableCell>
                    <TableCell className="text-right font-mono tabular-nums">{provider.consecutive_failures}</TableCell>
                    <TableCell className="font-mono text-xs tabular-nums">{formatJstDateTime(provider.failing_since)}</TableCell>
                    <TableCell className="max-w-md truncate pr-5 text-xs text-muted-foreground sm:pr-6" title={provider.last_error ?? undefined}>
                      {provider.last_error ?? EMPTY}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
          {neverAttempted.length > 0 && (
            <div className="grid gap-1.5 border-t px-5 py-4 sm:px-6">
              <span className="text-xs font-semibold text-muted-foreground">
                取得記録なし（{neverAttempted.length} 系列）
              </span>
              <p className="text-xs text-muted-foreground">
                一度も取得を試みていない系列。refresh がその group の途中で落ちると残りは記録を残さない。
              </p>
              <div className="flex flex-wrap gap-1.5">
                {neverAttempted.map((seriesId) => (
                  <Badge className="font-mono text-[10px]" key={seriesId} variant="outline">{seriesId}</Badge>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </SectionCard>
  )
}

export function SystemPage() {
  const [data, setData] = useState<SystemView | null>(null)
  const [run, setRun] = useState<WorkflowRunSummaryView | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    // Neither view is fatal. Deploy and materialize are independent steps, so a
    // freshly deployed UI routinely runs against serving objects that predate it;
    // failing the page would hide the run card, which is the part that says why.
    Promise.allSettled([
      fetchJson<SystemView>('/api/system'),
      fetchJson<WorkflowRunSummaryView>('/api/system/latest-run'),
    ]).then(([system, latestRun]) => {
      if (system.status === 'fulfilled') setData(system.value)
      else console.warn('system view unavailable', system.reason)
      if (latestRun.status === 'fulfilled') setRun(latestRun.value)
      else console.warn('latest run summary unavailable', latestRun.reason)
      setLoading(false)
    })
  }, [])

  if (loading) return <LoadingPage label="システム状態を読み込んでいます" />

  return (
    <PageShell
      lead="判断材料ではなく、パイプラインが動いているかどうか"
      meta={data !== null && (
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <span>store 状態の生成</span>
          <span className="font-mono tabular-nums">{formatJstDateTime(data.generated_at)}</span>
          {data.batch !== null && <Badge variant="secondary">{BATCH_KIND_LABEL[data.batch] ?? data.batch}</Badge>}
        </div>
      )}
      title="システム状態"
      width="reading"
    >
      <LatestRunCard run={run} />
      {data === null ? (
        <Card className="border-dashed shadow-none">
          <CardContent className="py-8 text-center text-sm text-muted-foreground">
            store の状態はまだ配信されていません。deploy 直後は materialize が走るまでこの状態になります。
          </CardContent>
        </Card>
      ) : (
        <>
          <StoresCard data={data} />
          <ProvidersCard data={data} />
        </>
      )}
    </PageShell>
  )
}
