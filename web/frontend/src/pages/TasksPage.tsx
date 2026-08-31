import { useEffect, useState } from 'react'
import { CircleAlert } from 'lucide-react'
import { Link } from 'react-router'

import { fetchJson } from '../api/client'
import type { OperationSessionView, OperationsView, TaskView, TasksView, UpcomingEventView } from '../api/types'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageShell } from '../components/PageShell'
import { PageState } from '../components/PageState'
import { SectionCard } from '../components/SectionCard'
import { Alert, AlertDescription, AlertTitle } from '../components/ui/alert'
import { Badge } from '../components/ui/badge'
import { formatJstDate, formatJstDateShort, formatJstStamp } from '../lib/format'

const OPERATION_KIND_LABEL: Record<string, string> = {
  'capital-allocation': '資本配分評価',
  'position-review': 'ポジション評価',
}

const STATUS_LABEL: Record<string, string> = { active: '進行中', completed: '完了' }
const EVENT_KIND_LABEL: Record<UpcomingEventView['kind'], string> = { earnings: '決算', reservation_expiry: '予約期限' }

function NextTaskCard({ task }: { task: TaskView | null }) {
  return <SectionCard meta={task?.overdue === true && <Badge variant="destructive">期限超過</Badge>} padded title="次のタスク">{task === null ? <p className="text-sm text-muted-foreground">未完了のタスクはありません。</p> : <div className="flex flex-wrap items-center gap-x-4 gap-y-2"><time className="shrink-0 font-mono text-sm font-semibold" dateTime={task.due_date}>{formatJstDate(task.due_date)}</time><p className="min-w-[16rem] flex-1 font-medium">{task.title}</p></div>}</SectionCard>
}

function OperationCard({ operations, error }: { operations: OperationSessionView[]; error: string | null }) {
  return <SectionCard meta={error === null ? <Badge variant="secondary">{operations.length} 件</Badge> : null} title="運用状況">{error !== null ? <Alert className="m-5" variant="destructive"><CircleAlert /><AlertTitle>運用状況を読み込めません</AlertTitle><AlertDescription>{error}</AlertDescription></Alert> : operations.length === 0 ? <p className="px-5 py-6 text-sm text-muted-foreground">進行中または完了済みの運用はありません。</p> : <div className="divide-y">{operations.map((item) => <div className="flex flex-wrap items-center justify-between gap-3 px-5 py-3 sm:px-6" key={item.operation_id}><div className="min-w-0"><p className="font-medium">{OPERATION_KIND_LABEL[item.session_kind] ?? item.session_kind}{item.ticker && <span className="ml-2 font-mono text-xs text-muted-foreground">{item.ticker}</span>}</p><p className="truncate font-mono text-xs text-muted-foreground">{item.operation_id}</p></div><div className="flex items-center gap-3"><span className="font-mono text-xs text-muted-foreground">開始 {formatJstStamp(item.started_at)}</span><Badge variant="outline">{STATUS_LABEL[item.status] ?? item.status}</Badge></div></div>)}</div>}</SectionCard>
}

export function EventsCard({ events, ledgerError }: { events: UpcomingEventView[]; ledgerError: string | null }) {
  const meta = ledgerError === null
    ? <Badge variant="secondary">{events.length} 件</Badge>
    : <Badge variant="destructive">不完全</Badge>
  return <SectionCard description="決算・予約期限" meta={meta} title="今後 14 日のイベント">{ledgerError !== null && <Alert className="m-5" variant="destructive"><CircleAlert /><AlertTitle>イベント情報が不完全です</AlertTitle><AlertDescription>{ledgerError}</AlertDescription></Alert>}{events.length === 0 && ledgerError === null ? <p className="px-5 py-6 text-sm text-muted-foreground">今後 14 日のイベントはありません。</p> : <div className="divide-y">{events.map((event) => <div className="flex flex-wrap items-center gap-3 px-5 py-3 sm:px-6" key={`${event.kind}-${event.event_date}-${event.ticker ?? ''}`}><time className="min-w-[5.5rem] font-mono text-sm" dateTime={event.event_date}>{formatJstDateShort(event.event_date)}</time><Badge variant={event.days_until <= 1 ? 'destructive' : 'outline'}>{event.days_until === 0 ? '本日' : `あと ${event.days_until} 日`}</Badge><Badge variant="secondary">{EVENT_KIND_LABEL[event.kind]}</Badge>{event.ticker === null ? <span>{event.label}</span> : <Link className="font-medium hover:underline" to={`/securities/${event.ticker}`}><span className="font-mono">{event.ticker}</span>{event.label !== event.ticker && <span className="ml-2 text-muted-foreground">{event.label}</span>}</Link>}</div>)}</div>}</SectionCard>
}

function TaskList({ data }: { data: TasksView }) {
  return <SectionCard meta={<Badge variant="secondary">未完了 {data.open_tasks.length} 件</Badge>} title="タスク一覧">{!data.tasks_exist ? <p className="px-5 py-6 text-sm text-muted-foreground">タスクはまだ登録されていません。</p> : data.open_tasks.length === 0 ? <p className="px-5 py-6 text-sm text-muted-foreground">未完了のタスクはありません。</p> : <div className="divide-y">{data.open_tasks.map((task) => <article className="flex flex-wrap items-center gap-3 px-5 py-3 sm:px-6" key={task.task_id}><time className="min-w-[10rem] font-mono text-sm" dateTime={task.due_date}>{formatJstDate(task.due_date)}</time>{task.overdue && <Badge variant="destructive">期限超過</Badge>}<span className="min-w-[16rem] flex-1 text-sm font-medium">{task.title}</span></article>)}</div>}</SectionCard>
}

export function TasksPage() {
  const [data, setData] = useState<TasksView | null>(null)
  const [operations, setOperations] = useState<OperationsView | null>(null)
  const [operationsError, setOperationsError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    fetchJson<TasksView>('/api/tasks').then(setData).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : 'Tasks を読み込めませんでした'))
    fetchJson<OperationsView>('/api/operations').then(setOperations).catch((reason: unknown) => setOperationsError(reason instanceof Error ? reason.message : '運用状況を読み込めませんでした'))
  }, [])
  if (error !== null) return <PageState message={error} title="Tasks read error" />
  if (data === null) return <LoadingPage label="Tasks を読み込んでいます" />
  return <PageShell lead="次に行う判断、進行中の運用、近いイベントを確認するread-only workflow面です。" title="Tasks"><NextTaskCard task={data.next_task} /><OperationCard error={operationsError} operations={operations?.operations ?? []} /><EventsCard events={data.upcoming_events} ledgerError={data.ledger_error} /><TaskList data={data} /></PageShell>
}
