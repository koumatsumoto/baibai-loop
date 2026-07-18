import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { ApiError, fetchJson } from '../api/client'
import type { CandidateRowView, SecurityDetailView } from '../api/types'
import { AppShell } from '../components/AppShell'
import { AsOfBadge } from '../components/AsOfBadge'
import { PctBadge } from '../components/PctBadge'
import { YenAmount } from '../components/YenAmount'

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <div className="detail-field"><dt>{label}</dt><dd>{children}</dd></div>
}

function FractionMetric({ value }: { value: number | null }) {
  return <PctBadge value={value} fraction />
}

function ScreeningMetrics({ row }: { row: CandidateRowView }) {
  return <dl className="metric-grid">
    <Field label="時価総額"><span className="numeric">{row.market_cap_oku === null ? '—' : `${row.market_cap_oku.toLocaleString('ja-JP')} 億円`}</span></Field>
    <Field label="PER / forward"><span className="numeric">{row.per_trailing ?? '—'} / {row.per_forward ?? '—'}</span></Field>
    <Field label="PBR"><span className="numeric">{row.pbr ?? '—'}</span></Field>
    <Field label="配当利回り"><FractionMetric value={row.dividend_yield} /></Field>
    <Field label="E[r]"><FractionMetric value={row.er_annual} /></Field>
    <Field label="Net cash / MC"><FractionMetric value={row.net_cash_to_market_cap} /></Field>
    <Field label="FCF yield"><FractionMetric value={row.fcf_yield} /></Field>
    <Field label="20d"><FractionMetric value={row.price_change_20d} /></Field>
    <Field label="52w low gap"><FractionMetric value={row.gap_from_52w_low} /></Field>
    <Field label="次決算"><span>{row.next_earnings_date ?? '—'}</span></Field>
  </dl>
}

export function SecurityDetailPage() {
  const { ticker = '' } = useParams()
  const [data, setData] = useState<SecurityDetailView | null>(null)
  const [notFound, setNotFound] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setData(null)
    setNotFound(false)
    setError(null)
    fetchJson<SecurityDetailView>(`/api/securities/${encodeURIComponent(ticker)}`).then(setData).catch((reason: unknown) => {
      if (reason instanceof ApiError && reason.status === 404) setNotFound(true)
      else setError(reason instanceof Error ? reason.message : '銘柄情報を読み込めませんでした')
    })
  }, [ticker])

  if (notFound) return <><AppShell /><main className="page page--message"><p className="eyebrow">404 / {ticker}</p><h1>この銘柄の記録はありません</h1><p>保有、research、最新 screening のいずれにも見つかりませんでした。</p><Link to="/screening">Screening に戻る</Link></main></>
  if (error) return <><AppShell /><main className="page page--message"><p className="eyebrow">READ ERROR</p><h1>{ticker}</h1><p>{error}</p></main></>
  if (!data) return <><AppShell /><main className="page page--loading"><p className="eyebrow">SECURITY</p><h1>{ticker} を読み込み中…</h1></main></>

  const packet = data.latest_packet
  return (
    <>
      <AppShell />
      <main className="page detail-page">
        <div className="breadcrumb"><Link to="/screening">Screening</Link><span>/</span><span>{data.ticker}</span></div>
        <section className="security-heading"><div><p className="eyebrow">SECURITY DETAIL</p><h1><span>{data.ticker}</span>{data.company_name ?? '名称なし'}</h1><p>{data.sector ?? 'sector —'}</p></div><div className="security-flags">{data.holding && <span>保有</span>}{data.revisions.length > 0 && <span>research {data.revisions.length}</span>}</div></section>

        {data.holding && <section className="detail-card detail-card--holding">
          <div className="detail-card__heading"><div><p className="eyebrow">HOLDING</p><h2>現在の保有</h2></div><AsOfBadge value={data.holding.market_price_as_of} /></div>
          <dl className="metric-grid metric-grid--holding">
            <Field label="数量"><span className="numeric">{data.holding.quantity.toLocaleString('ja-JP')} 株</span></Field>
            <Field label="取得原価"><YenAmount value={data.holding.deployed_cost_yen} /></Field>
            <Field label="現在値"><span className="numeric">¥{Number(data.holding.market_price_yen).toLocaleString('ja-JP')}</span></Field>
            <Field label="評価額"><YenAmount value={data.holding.market_value_yen} /></Field>
            <Field label="含み損益"><YenAmount value={data.holding.unrealized_pnl_yen} sign /> <PctBadge value={data.holding.unrealized_pnl_pct} /></Field>
            <Field label="FV"><YenAmount value={data.holding.fair_value_yen} /></Field>
            <Field label="FV乖離"><PctBadge value={data.holding.fv_gap_pct} /></Field>
            <Field label="判断"><span className="recommendation">{data.holding.recommendation ?? '—'}</span></Field>
          </dl>
        </section>}

        <section className="detail-card research-card">
          <div className="detail-card__heading"><div><p className="eyebrow">LATEST RESEARCH</p><h2>最新の 5 年評価</h2></div>{packet && <time dateTime={packet.revision.as_of}>{packet.revision.as_of}</time>}</div>
          {!packet ? <div className="empty-panel">research 記録なし</div> : <>
            <div className="research-verdict"><div><span>RECOMMENDATION</span><strong>{packet.revision.recommendation}</strong></div><div><span>CONFIDENCE</span><strong>{packet.revision.confidence ?? '—'}</strong></div><div><span>FAIR VALUE</span><strong><YenAmount value={packet.revision.current_fair_value_yen} /></strong></div></div>
            <dl className="metric-grid research-metrics">
              <Field label="entry price basis"><YenAmount value={packet.entry_price_basis_yen} /></Field>
              <Field label="required 5y base CAGR"><PctBadge value={packet.required_5y_base_cagr_pct} /></Field>
              <Field label="permanent loss risks"><span className="numeric">{packet.permanent_loss_risk_count} axes</span></Field>
              <Field label="model"><span>{packet.revision.model_version ?? '—'}</span></Field>
            </dl>
            <div className="scenario-strip">{packet.scenarios.map((scenario) => <span key={`${scenario.name}-${scenario.horizon_years}`}>{scenario.name}<b>{scenario.horizon_years}Y</b></span>)}</div>
            <dl className="narrative-grid"><Field label="Permanent loss conclusion"><p>{packet.permanent_loss_conclusion ?? '—'}</p></Field><Field label="Strongest countercase"><p>{packet.strongest_countercase ?? '—'}</p></Field><Field label="Sizing action"><p>{packet.sizing_action ?? '—'}</p></Field></dl>
            <code className="packet-path">{packet.revision.packet_path}</code>
          </>}
        </section>

        <section className="detail-card">
          <div className="detail-card__heading"><div><p className="eyebrow">HISTORY</p><h2>Research 履歴</h2></div><span>{data.revisions.length} revisions</span></div>
          {data.revisions.length === 0 ? <div className="empty-panel">research 記録なし</div> : <div className="revision-list">{data.revisions.map((revision) => <article key={revision.packet_path}><time dateTime={revision.as_of}>{revision.as_of}</time><strong>{revision.recommendation}</strong><YenAmount value={revision.current_fair_value_yen} /><span>{revision.model_version ?? 'model —'}</span><span className={revision.review_path ? 'flag flag--on' : 'flag'}>{revision.review_path ? 'review 有' : 'review —'}</span></article>)}</div>}
        </section>

        {data.candidate_row && <section className="detail-card">
          <div className="detail-card__heading"><div><p className="eyebrow">LATEST SCREENING</p><h2>Screening 指標</h2></div>{data.candidate_run && <span>as of {data.candidate_run.asof_date}</span>}</div>
          <ScreeningMetrics row={data.candidate_row} />
        </section>}
      </main>
    </>
  )
}
