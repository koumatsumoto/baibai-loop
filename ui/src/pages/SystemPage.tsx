import { useEffect, useState } from 'react'
import { ExternalLink } from 'lucide-react'

import { ApiError, fetchJson } from '../api/client'
import type {
  RunBatchView,
  RunErrorView,
  RunOutcome,
  RunPublishState,
  SystemStoreName,
  SystemView,
  WorkflowRunSummaryView,
} from '../api/types'
import { AppShell } from '../components/AppShell'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageState } from '../components/PageState'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/card'
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
}

const PUBLISH_LABEL: Record<RunPublishState, string> = {
  not_generated: '未生成',
  generated: '生成のみ',
  upload_failed: 'upload 失敗',
  published: '公開済み',
}

const BATCH_KIND_LABEL = { daily: '日次バッチ', manual: '手動 materialize' } as const

function outcomeTone(outcome: RunOutcome): string {
  if (outcome === 'succeeded') return 'border-positive/50 text-positive'
  if (outcome === 'skipped_non_business_day') return 'text-muted-foreground'
  if (outcome === 'published_with_deferred_failure') return 'border-warning/50 text-warning'
  return 'border-destructive/50 text-destructive'
}

function formatBytes(value: number | null): string {
  if (value === null) return EMPTY
  const mib = value / 1024 / 1024
  return mib >= 1024 ? `${(mib / 1024).toFixed(1)} GiB` : `${mib.toFixed(1)} MiB`
}

function formatCount(value: number | null): string {
  return value === null ? EMPTY : value.toLocaleString('ja-JP')
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
      {errors.map((error) => (
        <li className="flex flex-wrap items-baseline gap-x-2 text-xs" key={`${error.stage}-${error.code}-${error.message}`}>
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
  const metrics = Object.entries(batch.metrics)
  return (
    <div className="grid gap-1.5 border-b py-3 last:border-0 last:pb-0">
      <div className="flex flex-wrap items-center gap-2">
        <strong className="font-mono text-sm">{batch.batch_name}</strong>
        <Badge variant={batch.status === 'ok' ? 'secondary' : 'outline'}>{batch.status}</Badge>
        <span className="text-xs text-muted-foreground">{batch.duration_seconds.toFixed(1)}s</span>
        <span className="text-xs text-muted-foreground">{batch.datasets.join(' / ')}</span>
      </div>
      {metrics.length > 0 && (
        <div className="flex flex-wrap gap-x-4 gap-y-1 font-mono text-xs text-muted-foreground">
          {metrics.map(([key, value]) => (
            <span key={key}>{key}=<span className="text-foreground">{String(value)}</span></span>
          ))}
        </div>
      )}
      <ErrorList errors={batch.errors} />
    </div>
  )
}

function LatestRunCard({ run }: { run: WorkflowRunSummaryView | null }) {
  if (run === null) {
    return (
      <Card className="gap-3 py-5 shadow-sm">
        <CardHeader className="px-5">
          <CardTitle className="text-base">直近の日次バッチ</CardTitle>
          <CardDescription>run の記録がまだありません。cloud で 1 回走ると表示されます。</CardDescription>
        </CardHeader>
        <CardContent className="px-5">
          <Button asChild size="sm" variant="outline">
            <a href={ACTIONS_URL} rel="noreferrer noopener" target="_blank"><ExternalLink />GitHub Actions で確認</a>
          </Button>
        </CardContent>
      </Card>
    )
  }

  const execution = run.execution
  const batches = execution.kind === 'available' ? (execution.summary?.batches ?? []) : []

  return (
    <Card className="gap-3 py-5 shadow-sm">
      <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3 px-5">
        <div>
          <CardTitle className="text-base">直近の日次バッチ</CardTitle>
          <CardDescription className="mt-1">{run.workflow} · {run.trigger} · attempt {run.run_attempt}</CardDescription>
        </div>
        <Badge className={cn('text-xs', outcomeTone(run.overall_outcome))} variant="outline">
          {OUTCOME_LABEL[run.overall_outcome]}
        </Badge>
      </CardHeader>
      <CardContent className="grid gap-4 px-5">
        <dl className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-4">
          <DetailRow label="対象日">{run.asof ?? EMPTY}</DetailRow>
          <DetailRow label="所要">{run.duration_seconds.toFixed(0)}s</DetailRow>
          <DetailRow label="公開状態">{PUBLISH_LABEL[run.publish_state]}</DetailRow>
          <DetailRow label="通知">{run.delivery.status}</DetailRow>
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
      </CardContent>
    </Card>
  )
}

function StoresCard({ data }: { data: SystemView }) {
  return (
    <Card className="gap-0 overflow-hidden py-0 shadow-sm">
      <CardHeader className="border-b px-5 py-5 sm:px-6">
        <CardTitle className="text-base">store の鮮度と規模</CardTitle>
        <CardDescription className="mt-1">
          日付は store が持つ最新データ、件数は代表テーブルの行数
        </CardDescription>
      </CardHeader>
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
                <TableCell className="pl-5 font-medium sm:pl-6">{STORE_LABEL[store.store].name}</TableCell>
                <TableCell className="font-mono tabular-nums">
                  {!store.exists
                    ? <span className="text-muted-foreground">store なし</span>
                    : store.latest_date !== null
                      ? formatJstDate(store.latest_date)
                      : store.updated_at !== null
                        ? formatJstDateTime(store.updated_at)
                        : EMPTY}
                </TableCell>
                <TableCell className="text-right font-mono tabular-nums">
                  {formatCount(store.row_count)}
                  {store.row_count !== null && <span className="ml-1 text-xs text-muted-foreground">{STORE_LABEL[store.store].rows}</span>}
                </TableCell>
                <TableCell className="pr-5 text-right font-mono tabular-nums sm:pr-6">{formatBytes(store.size_bytes)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </Card>
  )
}

function ProvidersCard({ data }: { data: SystemView }) {
  const failing = data.failing_providers
  return (
    <Card className="gap-0 overflow-hidden py-0 shadow-sm">
      <CardHeader className="flex flex-row items-start justify-between gap-4 border-b px-5 py-5 sm:px-6">
        <div>
          <CardTitle className="text-base">provider 取得の健全性</CardTitle>
          <CardDescription className="mt-1">
            直近の取得が失敗したままの系列。stale になる前に provider の停止を捉える
          </CardDescription>
        </div>
        <Badge variant={failing.length === 0 ? 'secondary' : 'destructive'}>
          {failing.length} / {data.provider_series_total} 系列
        </Badge>
      </CardHeader>
      {failing.length === 0 ? (
        <CardContent className="py-8 text-center text-sm text-muted-foreground">
          全 {data.provider_series_total} 系列の直近取得が成功しています。
        </CardContent>
      ) : (
        <div className="overflow-x-auto">
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
        </div>
      )}
    </Card>
  )
}

export function SystemPage() {
  const [data, setData] = useState<SystemView | null>(null)
  const [run, setRun] = useState<WorkflowRunSummaryView | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetchJson<SystemView>('/api/system').then(setData).catch((reason: unknown) => {
      setError(reason instanceof Error ? reason.message : 'システム状態を読み込めませんでした')
    })
    // The run summary exists only in cloud, and only after a run published one.
    // A 404 is the normal local / first-run state, not a page error.
    fetchJson<WorkflowRunSummaryView>('/api/system/latest-run')
      .then(setRun)
      .catch((reason: unknown) => {
        if (!(reason instanceof ApiError) || reason.status !== 404) {
          console.warn('latest run summary unavailable', reason)
        }
        setRun(null)
      })
  }, [])

  if (error) return <PageState message={error} title="System read error" />
  if (!data) return <LoadingPage label="システム状態を読み込んでいます" />

  return (
    <>
      <AppShell />
      <main className="mx-auto grid max-w-[1200px] gap-5 px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
        <header className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">システム状態</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              判断材料ではなく、パイプラインが動いているかどうか
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <span>この画面のデータ</span>
            <span className="font-mono tabular-nums">{formatJstDateTime(data.generated_at)}</span>
            {data.batch !== null && <Badge variant="secondary">{BATCH_KIND_LABEL[data.batch]}</Badge>}
          </div>
        </header>

        <LatestRunCard run={run} />
        <StoresCard data={data} />
        <ProvidersCard data={data} />
      </main>
    </>
  )
}
