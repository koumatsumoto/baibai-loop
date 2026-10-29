import { useEffect, useState } from 'react'
import { CircleAlert } from 'lucide-react'

import { fetchJson } from '../api/client'
import type { TaskView, TasksView, UpcomingEventView } from '../api/types'
import { LoadingPage } from '../components/LoadingIndicator'
import { PageShell } from '../components/PageShell'
import { PageState } from '../components/PageState'
import { SectionCard } from '../components/SectionCard'
import { Alert, AlertDescription, AlertTitle } from '../components/ui/alert'
import { Badge } from '../components/ui/badge'
import { daysUntilJstDate, formatJstDate, formatJstDateShort, formatJstDateTime } from '../lib/format'
import { useJstToday } from '../lib/use-jst-today'

const EVENT_KIND_LABEL: Record<UpcomingEventView['kind'], string> = { earnings: '決算', reservation_expiry: '予約期限' }

export function NextTaskCard({ task, today }: { task: TaskView | null; today: string }) {
  return <SectionCard meta={task !== null && task.due_date < today && <Badge variant="destructive">期限超過</Badge>} padded title="期限が近い用件">{task === null ? <p className="text-sm text-muted-foreground">未完了のタスクはありません。</p> : <div className="flex flex-wrap items-center gap-x-4 gap-y-2"><time className="shrink-0 font-mono text-sm font-semibold" dateTime={task.due_date}>期限 {formatJstDate(task.due_date)}</time><p className="min-w-[16rem] flex-1 font-medium">{task.title}</p></div>}</SectionCard>
}

export function EventsCard({ events, ledgerError, earningsCalendarAvailable = null, today }: {
  events: UpcomingEventView[]
  ledgerError: string | null
  earningsCalendarAvailable?: boolean | null
  today: string
}) {
  const complete = ledgerError === null && earningsCalendarAvailable !== false
  const meta = complete
    ? <Badge variant="secondary">{events.length} 件</Badge>
    : <Badge variant="destructive">不完全</Badge>
  return (
    <SectionCard description="表示データ生成時点で今後14日の予定。日数は今日（JST）基準です。" meta={meta} title="決算・予約期限のイベント">
      {ledgerError !== null && (
        <Alert className="m-5" variant="destructive">
          <CircleAlert /><AlertTitle>イベント情報が不完全です</AlertTitle>
          <AlertDescription>{ledgerError}</AlertDescription>
        </Alert>
      )}
      {earningsCalendarAvailable === false && (
        <Alert className="m-5" variant="destructive">
          <CircleAlert /><AlertTitle>決算予定を確認できません</AlertTitle>
          <AlertDescription>有効な決算カレンダーを確認できていません。確認できた予約期限だけを表示しています。</AlertDescription>
        </Alert>
      )}
      {events.length === 0 && complete ? (
        <p className="px-5 py-6 text-sm text-muted-foreground">表示データにイベントはありません。</p>
      ) : (
        <div className="divide-y">
          {events.map((event) => {
            const daysUntil = daysUntilJstDate(event.event_date, today)
            return (
              <div className="flex flex-wrap items-center gap-3 px-5 py-3 sm:px-6" key={`${event.kind}-${event.event_date}-${event.ticker ?? ''}`}>
                <time className="min-w-[5.5rem] font-mono text-sm" dateTime={event.event_date}>{formatJstDateShort(event.event_date)}</time>
                <Badge variant={daysUntil <= 1 ? 'destructive' : 'outline'}>{daysUntil === 0 ? '本日' : daysUntil < 0 ? `${-daysUntil} 日前` : `あと ${daysUntil} 日`}</Badge>
                <Badge variant="secondary">{EVENT_KIND_LABEL[event.kind]}</Badge>
                {event.ticker === null ? <span>{event.label}</span> : (
                  <span className="font-medium">
                    <span className="font-mono">{event.ticker}</span>
                    {event.label !== event.ticker && <span className="ml-2 text-muted-foreground">{event.label}</span>}
                  </span>
                )}
              </div>
            )
          })}
        </div>
      )}
    </SectionCard>
  )
}

export function TaskList({ data, today }: { data: TasksView; today: string }) {
  return <SectionCard meta={<Badge variant="secondary">未完了 {data.open_tasks.length} 件</Badge>} title="タスク一覧">{!data.tasks_exist ? <p className="px-5 py-6 text-sm text-muted-foreground">タスクはまだ登録されていません。</p> : data.open_tasks.length === 0 ? <p className="px-5 py-6 text-sm text-muted-foreground">未完了のタスクはありません。</p> : <div className="divide-y">{data.open_tasks.map((task) => <article className="flex flex-wrap items-center gap-3 px-5 py-3 sm:px-6" key={task.task_id}><time className="min-w-[10rem] font-mono text-sm" dateTime={task.due_date}>期限 {formatJstDate(task.due_date)}</time>{task.due_date < today && <Badge variant="destructive">期限超過</Badge>}<span className="min-w-[16rem] flex-1 text-sm font-medium">{task.title}</span></article>)}</div>}</SectionCard>
}

export function TasksPage() {
  const [data, setData] = useState<TasksView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const today = useJstToday()
  useEffect(() => {
    const controller = new AbortController()
    fetchJson<TasksView>('/api/tasks', { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setData(value) })
      .catch((reason: unknown) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : 'Tasks を読み込めませんでした') })
    return () => controller.abort()
  }, [])
  if (error !== null) return <PageState message={error} title="Tasks read error" />
  if (data === null) return <LoadingPage label="Tasks を読み込んでいます" />
  return <PageShell lead="期限付きの用件と、近い決算・予約期限を確認します。" meta={<span className="text-xs text-muted-foreground">表示データ生成 <time dateTime={data.generated_at}>{formatJstDateTime(data.generated_at)} JST</time></span>} title="Tasks">
    <NextTaskCard task={data.next_task} today={today} />
    <EventsCard events={data.upcoming_events} ledgerError={data.ledger_error} earningsCalendarAvailable={data.earnings_calendar_available} today={today} />
    <TaskList data={data} today={today} />
  </PageShell>
}
