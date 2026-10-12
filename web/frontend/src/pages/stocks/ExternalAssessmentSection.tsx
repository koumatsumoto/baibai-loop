import type { ExternalAssessment, MetricComparison, ModelValuationObservation } from '../../api/types'
import { SectionCard } from '../../components/SectionCard'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../../components/ui/table'

const LABELS: Record<string, string> = {
  consensus_target: '目標株価コンセンサス', per_company_forecast: '会社予想PERモデル価格',
  per_consensus: 'アナリスト予想PERモデル価格', pbr: 'PBRモデル価格', rating: 'アナリスト rating',
  growth_score: '成長性', undervaluation_score: '割安性', size_score: '企業規模',
  technical_score: 'テクニカル', financial_health_score: '財務健全性', market_trend_score: '市場トレンド',
  composite_score: '媒体の総合score', industry_average_score: '媒体の業種平均score',
  price_target_average: '目標株価平均', price_target_high: '目標株価上限', price_target_low: '目標株価下限',
  eps_forecast_next_fq: '次四半期EPS予測', eps_forecast_next_fy: '次年度EPS予測',
  revenue_forecast_next_fq: '次四半期売上予測', revenue_forecast_next_fy: '次年度売上予測',
  forward_pe: '予想PER', forward_ps: '予想P/S', forward_ev_ebitda: '予想EV/EBITDA',
  ebitda_estimate_fy: '年度EBITDA予測', free_cash_flow_estimate_fy: '年度FCF予測',
  capex_estimate_fy: '年度設備投資予測', total_debt_estimate_fy: '年度負債予測',
}
const STATES: Record<MetricComparison['state'], string> = {
  not_observed: '未取得', first_observation: '初回受領', missing: '最新は欠測', recovered: '欠測から復帰',
  count_changed: '人数が変化', not_comparable: '比較基準が未確認・変化',
  same_value: '同値の再観測', increased: '提示値が増加', decreased: '提示値が減少',
}
function number(value: number | null): string { return value === null ? '—' : value.toLocaleString('ja-JP') }
function known(value: string | number | null | undefined): string { return value == null || value === '' ? '未確認' : String(value) }
function Metrics({ comparisons, notes = {} }: { comparisons: MetricComparison[]; notes?: Record<string, string> }) {
  return <Table><TableHeader><TableRow><TableHead>提示値</TableHead><TableHead className="text-right">最新</TableHead><TableHead className="text-right">直前</TableHead><TableHead>受領間の比較</TableHead></TableRow></TableHeader><TableBody>{comparisons.map(item => <TableRow key={item.metric}>
    <TableCell>{LABELS[item.metric] ?? item.metric}{notes[item.metric] && <p className="text-xs text-muted-foreground">{notes[item.metric]}</p>}</TableCell><TableCell className="text-right font-mono">{number(item.latest_value)}</TableCell><TableCell className="text-right font-mono">{number(item.previous_value)}</TableCell>
    <TableCell>{STATES[item.state]}{item.comparable && `（差分 ${number(item.delta)}）`}</TableCell>
  </TableRow>)}</TableBody></Table>
}
function modelNotes(row: ModelValuationObservation): Record<string, string> {
  const entries = {
    consensus_target: [row.consensus_target_origin, row.consensus_target_period, row.consensus_target_updated_at_utc ?? row.consensus_target_updated_label, row.consensus_target_count],
    per_company_forecast: [row.per_company_forecast_origin, row.per_company_forecast_period, row.per_company_forecast_updated_at_utc ?? row.per_company_forecast_updated_label, row.per_company_forecast_count],
    per_consensus: [row.per_consensus_origin, row.per_consensus_period, row.per_consensus_updated_at_utc ?? row.per_consensus_updated_label, row.per_consensus_count],
    pbr: [row.pbr_origin, row.pbr_period, row.pbr_updated_at_utc ?? row.pbr_updated_label, row.pbr_count],
  }
  return Object.fromEntries(Object.entries(entries).map(([metric, [origin, period, updated, count]]) => [metric, `原評価元 ${known(origin)} · 対象期間 ${known(period)} · 提供元更新 ${known(updated)} · 寄与者数 ${known(count)}`]))
}
function Receipt({ day, fetched, previous }: { day: string; fetched: string; previous?: string }) {
  return <p className="text-xs text-muted-foreground">受領日(JST) {day} · 取得(UTC) {fetched}{previous && ` · 直前取得 ${previous}`}</p>
}

export function ExternalAssessmentSection({ assessment }: { assessment: ExternalAssessment | null | undefined }) {
  if (!assessment) return <SectionCard title="外部参考評価" padded><p className="text-sm text-muted-foreground">保存済み外部観測なし</p></SectionCard>
  const observed = assessment.model_valuations.length + assessment.analyst_ratings.length + assessment.quants_scores.length > 0 || assessment.tradingview.inputs.latest !== null
  const tv = assessment.tradingview.inputs.latest
  return <SectionCard title="外部参考評価" description="媒体が提示した値です。独自の企業価値評価とは分け、異なる尺度を多数決点へまとめません。" meta={<span className="text-xs">読取対象時点(UTC) {assessment.cutoff}</span>} padded>
    {!observed && <p className="text-sm text-muted-foreground">この対象時点以前の保存済み外部評価はありません。</p>}
    <div className="grid gap-6">
      {assessment.model_valuations.map(source => {
        const row = source.inputs.latest
        if (!row) return null
        return <div key={row.distributor} className="grid gap-2"><h3 className="text-sm font-medium">{row.distributor} · 外部モデル価格</h3>
          <Receipt day={row.snapshot_date} fetched={row.fetched_at_utc} previous={source.inputs.previous?.fetched_at_utc} />
          <p className="text-xs">通貨 {known(row.currency)} · 単位 {known(row.unit)} · 株式basis {known(row.share_basis)}</p>
          <Metrics comparisons={source.comparisons} notes={modelNotes(row)} /><a className="text-xs underline" href={row.source_url} rel="noopener noreferrer" target="_blank">掲載元</a>
        </div>
      })}
      {assessment.analyst_ratings.map(source => {
        const row = source.inputs.latest
        if (!row) return null
        return <div key={row.distributor} className="grid gap-2"><h3 className="text-sm font-medium">{row.distributor} · アナリスト rating</h3>
          <Receipt day={row.snapshot_date} fetched={row.fetched_at_utc} previous={source.inputs.previous?.fetched_at_utc} />
          <p className="text-xs">原評価元 {known(row.origin)} · 尺度 {known(row.rating_scale_min)}〜{known(row.rating_scale_max)} · 方向 {row.rating_scale_direction === 'higher_is_positive' ? '高いほど肯定的' : row.rating_scale_direction === 'lower_is_positive' ? '低いほど肯定的' : '未確認'} · 人数 {known(row.rating_count)} · 提供元更新 {known(row.rating_updated_at_utc ?? row.rating_updated_label)}</p>
          <Metrics comparisons={[source.comparison]} /><a className="text-xs underline" href={row.source_url} rel="noopener noreferrer" target="_blank">掲載元</a>
        </div>
      })}
      {assessment.quants_scores.map(source => {
        const row = source.inputs.latest
        if (!row) return null
        return <div key={row.distributor} className="grid gap-2"><h3 className="text-sm font-medium">株マップ · Quants Research score</h3>
          <Receipt day={row.snapshot_date} fetched={row.fetched_at_utc} previous={source.inputs.previous?.fetched_at_utc} />
          <p className="text-xs">提示基準日 {row.score_as_of_date} · 直前提示基準日 {known(source.inputs.previous?.score_as_of_date)} · 6軸は{row.score_scale_max}点、媒体総合・業種平均は{row.composite_scale_max}点満点。rating・目標株価へ換算しません。</p>
          <Metrics comparisons={source.comparisons} /><a className="text-xs underline" href={row.source_url} rel="noopener noreferrer" target="_blank">掲載元</a>
        </div>
      })}
      {tv && <div className="grid gap-2"><h3 className="text-sm font-medium">TradingView · 外部予測欄</h3>
        <Receipt day={tv.snapshot_date} fetched={tv.fetched_at_utc} previous={assessment.tradingview.inputs.previous?.fetched_at_utc} />
        <p className="text-xs">{assessment.tradingview.availability === 'unresolved' ? '応答内で未解決と記録された行です。' : assessment.tradingview.availability === 'all_null' ? '受領済みですが、予測欄には値がありません。' : '受領済みで予測欄に値があります。未確認の評価基準は補完しません。'}</p>
        <p className="text-xs">予測の通貨 {known(tv.estimate_currency)} · 単位 {known(tv.estimate_unit)} · 対象期 {known(tv.target_period_label)} / {known(tv.target_period_end)} · 提供元更新 {known(tv.provider_updated_at_utc)}</p>
        <p className="text-xs">原評価元・株式基準は未確認です。次年度予測と年度estimateの絶対対象期が同じとは限りません。</p>
        <p className="text-xs">参考close {number(tv.quote_close)} {known(tv.quote_currency)} · quote時刻 {known(tv.quote_at_utc)} · rating {known(tv.analyst_rating)} · recommendation人数 {known(tv.recommendation_total)}（各予測欄の寄与者数とは限りません）</p>
        <Metrics comparisons={assessment.tradingview.comparisons} />
      </div>}
      {assessment.origin_groups.length > 0 && <div className="text-xs text-muted-foreground"><p>同じ原評価元の提示です。独立票として合算しません。</p>{assessment.origin_groups.map(group => <p key={group.origin}>{group.origin}: {group.references.map(ref => `${ref.distributor} / ${LABELS[ref.metric] ?? ref.metric}`).join('、')}</p>)}</div>}
    </div>
  </SectionCard>
}
