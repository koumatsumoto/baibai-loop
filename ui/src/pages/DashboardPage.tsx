import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { fetchJson } from '../api/client'
import type { DashboardView, TaskView } from '../api/types'
import { AppShell } from '../components/AppShell'
import { AsOfBadge } from '../components/AsOfBadge'
import { PctBadge } from '../components/PctBadge'
import { YenAmount } from '../components/YenAmount'

function formatDate(value: string | null) {
  if (value === null) return '日時なし'
  return new Intl.DateTimeFormat('ja-JP', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    weekday: 'short',
  }).format(new Date(`${value}T00:00:00+09:00`))
}

function TaskCard({
  label,
  task,
  event = false,
}: {
  label: string
  task: TaskView | null
  event?: boolean
}) {
  const date = event ? task?.event_date ?? task?.due_date ?? null : task?.due_date ?? null
  return (
    <article className="next-card">
      <div className="next-card__label">
        <span>{label}</span>
        {task?.overdue && <span className="overdue">期限超過</span>}
      </div>
      {task ? (
        <>
          <strong>{event && task.event_label ? task.event_label : task.title}</strong>
          <time dateTime={date ?? undefined}>{formatDate(date)}</time>
          {event && <span className="next-card__context">{task.title}</span>}
        </>
      ) : (
        <strong className="muted">なし</strong>
      )}
    </article>
  )
}

function SummaryCard({
  label,
  value,
  detail,
}: {
  label: string
  value: number | null
  detail?: string
}) {
  return (
    <article className="summary-card">
      <span>{label}</span>
      <strong><YenAmount value={value} /></strong>
      {detail && <small>{detail}</small>}
    </article>
  )
}

export function DashboardPage() {
  const [data, setData] = useState<DashboardView | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetchJson<DashboardView>('/api/dashboard').then(setData).catch((reason: unknown) => {
      setError(reason instanceof Error ? reason.message : 'Dashboard を読み込めませんでした')
    })
  }, [])

  if (error) {
    return <main className="page page--message"><p className="eyebrow">READ ERROR</p><h1>Dashboard</h1><p>{error}</p></main>
  }
  if (!data) {
    return <main className="page page--loading"><p className="eyebrow">BAIBAI-LOOP</p><h1>資産状況を読み込み中…</h1></main>
  }

  return (
    <>
      <AppShell />

      <main className="page dashboard">
        {data.ledger_error && <div className="alert alert--danger"><strong>Ledger error</strong><span>{data.ledger_error}</span></div>}
        <section className="page-heading">
          <div><p className="eyebrow">OPERATIONS COCKPIT</p><h1>いま、何をすべきか。</h1></div>
          <AsOfBadge value={data.ledger_as_of} stale={data.ledger_stale} />
        </section>

        <section className="summary-grid" aria-label="資産サマリ">
          <SummaryCard label="総資産" value={data.total_capital_yen} detail={`${data.holdings.length} 銘柄を保有`} />
          <SummaryCard label="現金" value={data.available_cash_yen} detail={data.cash_pct === null ? undefined : `${data.cash_pct.toFixed(1)}% available`} />
          <SummaryCard label="予約" value={data.reserved_cash_yen} detail={data.reserved_pct === null ? undefined : `${data.reserved_pct.toFixed(1)}% reserved`} />
          <SummaryCard label="保有評価額" value={data.holdings_market_value_yen} detail={data.deployed_pct === null ? undefined : `${data.deployed_pct.toFixed(1)}% deployed`} />
        </section>

        <div className="allocation" aria-label="資産配分">
          <span className="allocation__cash" style={{ width: `${data.cash_pct ?? 0}%` }} />
          <span className="allocation__reserved" style={{ width: `${data.reserved_pct ?? 0}%` }} />
          <span className="allocation__deployed" style={{ width: `${data.deployed_pct ?? 0}%` }} />
        </div>

        <section className="next-grid" aria-label="次のアクション">
          <TaskCard label="NEXT TASK" task={data.next_task} />
          <TaskCard label="NEXT EVENT" task={data.next_event} event />
        </section>

        {data.warnings.length > 0 && (
          <section className="alerts" aria-label="portfolio warnings">
            {data.warnings.map((warning) => (
              <div className="alert" key={`${warning.code}-${warning.scope}-${warning.key}`}>
                <strong>{warning.code}</strong>
                <span>{warning.scope}: {warning.key}</span>
                <span className="numeric">{warning.actual_pct.toFixed(2)}% / warning {warning.warning_pct.toFixed(2)}%</span>
              </div>
            ))}
          </section>
        )}

        {!data.ledger_exists && !data.ledger_error && <div className="empty-panel">portfolio ledger がありません。</div>}

        <section className="section-block">
          <div className="section-heading"><div><p className="eyebrow">PORTFOLIO</p><h2>保有銘柄</h2></div><span>{data.holdings.length} positions</span></div>
          {data.holdings.length === 0 ? <div className="empty-panel">保有銘柄はありません。</div> : (
            <div className="table-wrap">
              <table>
                <thead><tr><th>銘柄</th><th>sector</th><th className="right">数量</th><th className="right">取得原価</th><th className="right">現在値 / as of</th><th className="right">評価額</th><th className="right">含み損益</th><th className="right">FV</th><th className="right">FV乖離</th><th>判断</th></tr></thead>
                <tbody>
                  {data.holdings.map((holding) => (
                    <tr key={holding.ticker}>
                      <td><Link className="ticker-link" to={`/securities/${holding.ticker}`}>{holding.ticker}</Link><span className="company-name">{holding.company_name ?? '—'}</span></td>
                      <td>{holding.sector}</td>
                      <td className="right numeric">{holding.quantity.toLocaleString('ja-JP')}</td>
                      <td className="right"><YenAmount value={holding.deployed_cost_yen} /></td>
                      <td className="right"><span className="numeric">¥{Number(holding.market_price_yen).toLocaleString('ja-JP')}</span><AsOfBadge value={holding.market_price_as_of} compact /></td>
                      <td className="right"><YenAmount value={holding.market_value_yen} /></td>
                      <td className="right"><YenAmount value={holding.unrealized_pnl_yen} sign /><PctBadge value={holding.unrealized_pnl_pct} /></td>
                      <td className="right"><YenAmount value={holding.fair_value_yen} /></td>
                      <td className="right"><PctBadge value={holding.fv_gap_pct} /></td>
                      <td><span className="recommendation">{holding.recommendation ?? '—'}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        {data.reservations.length > 0 && (
          <section className="section-block">
            <div className="section-heading"><div><p className="eyebrow">RESERVATIONS</p><h2>資金予約</h2></div></div>
            <div className="compact-list">
              {data.reservations.map((reservation) => <div key={reservation.reservation_id}><strong>{reservation.ticker}</strong><span>{reservation.remaining_quantity} 株 × ¥{Number(reservation.price_guard_yen).toLocaleString('ja-JP')}</span><YenAmount value={reservation.reserved_yen} /><AsOfBadge value={reservation.expires_at} compact /></div>)}
            </div>
          </section>
        )}

        <section className="section-block">
          <div className="section-heading"><div><p className="eyebrow">AGENDA</p><h2>Open tasks</h2></div><span>{data.open_tasks.length} open</span></div>
          {!data.tasks_exist ? <div className="empty-panel">task record 未作成（records/05-task/tasks.yaml）</div> : data.open_tasks.length === 0 ? <div className="empty-panel">open task はありません。</div> : (
            <div className="task-list">{data.open_tasks.map((task) => <article key={task.task_id}><time dateTime={task.due_date}>{formatDate(task.due_date)}</time><div><strong>{task.title}</strong><span>{task.event_label ?? task.kind}</span></div>{task.overdue && <span className="overdue">期限超過</span>}</article>)}</div>
          )}
        </section>
      </main>
    </>
  )
}
