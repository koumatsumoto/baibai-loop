import { Link, useLocation } from 'react-router'

import type { StockAnalysisRowView, ScreeningRunView, CandidateDiscoverySummaryView } from '../../api/types'
import { AsOfBadge } from '../../components/AsOfBadge'
import { SectionCard } from '../../components/SectionCard'
import { UpdatedAtBadge } from '../../components/UpdatedAtBadge'
import { TradingViewWatchlistExportButton } from '../../components/TradingViewWatchlistExportButton'
import { Badge } from '../../components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../../components/ui/table'
import { EMPTY, formatNumber } from '../../lib/format'
import { valuationApproachLabel } from '../../lib/stocks'

function number(value: number | null, digits = 2): string {
  return value === null ? EMPTY : formatNumber(value, digits)
}

function percent(value: number | null): string {
  return value === null ? EMPTY : `${formatNumber(value * 100, 1)}%`
}

function earningsStatusLabel(status: string | null): string | null {
  switch (status) {
    case 'scheduled': return '決算予定'
    case 'scheduled_date_reached':
    case 'announced': return '予定日到来・経過（発表未確認）'
    case 'estimated': return '次回予定推定'
    case 'unknown': return '予定未確認'
    case null: return null
    default: return '予定状態未確認'
  }
}

function entryNotes(entry: StockAnalysisRowView) {
  const quality = entry
  const context = entry
  const flags = [
    quality.stale_fin_flag === true ? '予定日到来・財務未照合' : null,
    quality.bs_carry_forward_fields,
    quality.edinet_failure_reasons,
    earningsStatusLabel(context.next_earnings_status),
    context.tse_capital_policy_status,
    context.large_holding_filing_within_lookback === true ? 'large_holding_filing' : null,
    context.tender_offer_filing_within_lookback === true ? 'tender_offer_filing' : null,
  ].filter((item): item is string => item !== null && item !== '')
  return flags.length === 0 ? <span className="text-muted-foreground">{EMPTY}</span> : (
    <div className="flex max-w-72 flex-wrap gap-1">
      {flags.map((flag, index) => <Badge key={`${flag}-${index}`} variant="outline">{flag}</Badge>)}
    </div>
  )
}

function FrozenGroup({ title, fields }: { title: string; fields: readonly (readonly [string, string])[] }) {
  return (
    <section>
      <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{title}</h4>
      <dl className="mt-2 grid gap-x-5 gap-y-2 sm:grid-cols-2 lg:grid-cols-3">
        {fields.map(([label, value]) => <div key={label}><dt className="text-[10px] text-muted-foreground">{label}</dt><dd className="font-mono text-xs tabular-nums break-words whitespace-normal">{value}</dd></div>)}
      </dl>
    </section>
  )
}

function FrozenAnalysis({ analysis }: { analysis: StockAnalysisRowView }) {
  const identity = analysis
  const valuation = analysis
  const current = analysis
  const normalized = analysis
  const asset = analysis
  const reinvestment = analysis
  const expected = analysis
  const quality = analysis
  const context = analysis
  return (
    <div className="grid gap-5 py-3">
      <FrozenGroup fields={[
        ['market cap', `${number(identity.market_cap_oku)} 億円`], ['ADV (context)', `${number(identity.avg_turnover_oku)} 億円`],
        ['listing span', `${number(identity.listing_span_days, 0)} 日`], ['JPX flags', identity.jpx_flags?.join(', ') || EMPTY],
      ]} title="Identity / liquidity" />
      <FrozenGroup fields={[
        ['PER forward', number(valuation.per_forward)], ['PER trailing', number(valuation.per_trailing)], ['PBR', number(valuation.pbr)],
        ['EV/EBITDA', number(valuation.ev_ebitda)], ['P/S', number(valuation.p_s)], ['PCFR', number(valuation.pcfr)],
      ]} title="Valuation" />
      <FrozenGroup fields={[
        ['FCF yield', percent(current.fcf_yield)], ['OCF yield', percent(current.ocf_yield)],
        ['special gain flag', String(current.forecast_special_gain_flag ?? EMPTY)], ['full-year loss flag', String(current.forecast_full_year_loss_flag ?? EMPTY)],
      ]} title="Current earnings" />
      <FrozenGroup fields={[
        ['3FY平均EPSベースPER', number(normalized.normalized_per_3fy)], ['sector gap', number(normalized.candidate.normalized_per_3fy_sector_gap)],
      ]} title="3FY平均EPS" />
      <FrozenGroup fields={[
        ['asset-backed ratio', percent(asset.asset_backed_ratio)], ['Net cash / MC', percent(asset.net_cash_to_market_cap)],
        ['investment securities', number(asset.investment_securities)], ['equity ratio', percent(asset.equity_ratio)],
      ]} title="Asset value" />
      <FrozenGroup fields={[
        ['P/S sector gap', number(reinvestment.p_s_sector_gap)], ['capital return proxy', percent(reinvestment.operating_return_on_capital_proxy)],
        ['sales YoY', percent(reinvestment.sales_yoy)], ['operating margin', percent(reinvestment.operating_margin)], ['FCF yield', percent(reinvestment.fcf_yield)],
      ]} title="Reinvestment" />
      <FrozenGroup fields={[
        ['machine E[r] annual', percent(expected.er_annual)], ['reversion', percent(expected.er_reversion_annual)], ['carry', percent(expected.er_carry_annual)],
        ['配当利回り入力', expected.dividend_yield == null ? '未観測・未記録' : percent(expected.dividend_yield)],
        ['配当basis', expected.dividend_basis ?? '未記録'],
        ['発行済株式数YoY入力', expected.net_share_change_yoy == null ? '未観測・未記録' : percent(expected.net_share_change_yoy)],
        ['carryの欠測', expected.carry_missing_assumption == null ? 'この保存snapshotには仮定の記録がありません。' : '欠測の成分は計算上0寄与。無配・株数不変の確認ではありません。'],
        ['機械・業種中央値 anchor', `${number(expected.fv_sector_median_yen, 0)} 円`], ['機械・自己レンジ anchor', `${number(expected.fv_self_range_yen, 0)} 円`],
        ['origin', expected.er_origin ?? EMPTY], ['model', expected.er_model_version ?? EMPTY], ['unit', expected.er_unit ?? EMPTY], ['assumptions', expected.er_assumptions ?? EMPTY],
      ]} title="Machine return prior (secondary context)" />
      <FrozenGroup fields={[
        ['BS carry fields', quality.bs_carry_forward_fields ?? EMPTY], ['BS carry lag', `${number(quality.bs_carry_forward_lag_days, 0)} 日`],
        ['EDINET failures', quality.edinet_failure_reasons ?? EMPTY], ['予定日到来・財務未照合', String(quality.stale_fin_flag ?? EMPTY)],
        ...(quality.ttm_quality_ev_ebitda && quality.ttm_quality_ev_ebitda !== 'exact' ? [['EV/EBITDA TTM', quality.ttm_quality_ev_ebitda] as const] : []),
        ...(quality.ttm_quality_fcf && quality.ttm_quality_fcf !== 'exact' ? [['FCF TTM', quality.ttm_quality_fcf] as const] : []),
      ]} title="Data quality" />
      <FrozenGroup fields={[
        ['決算予定の状態', earningsStatusLabel(context.next_earnings_status) ?? EMPTY], ['次回予定推定日', context.next_earnings_estimated_date ?? EMPTY],
        ['margin short / ADV', number(context.margin_short_to_adv)], ['TSE capital policy', context.tse_capital_policy_status ?? EMPTY],
        ['large holding filing', String(context.large_holding_filing_within_lookback ?? EMPTY)],
        ['tender offer filing', String(context.tender_offer_filing_within_lookback ?? EMPTY)],
      ]} title="Context" />
    </div>
  )
}

export function CandidateSection({ rows, run, method }: { rows: StockAnalysisRowView[]; run: ScreeningRunView | null; method: CandidateDiscoverySummaryView | null }) {
  const candidates = rows.filter(row => row.candidate.reasons.length > 0).sort((a, b) => a.ticker.localeCompare(b.ticker))
  const runAsOf = run?.as_of ?? '未作成'
  const { pathname, search } = useLocation()
  return (
    <SectionCard
      description="各Approachの推薦を統合した集合。3FY平均EPSは一時損益・循環を調整した代表利益ではありません。利益倍率の探索でCFは同順位時の補助です。表示値は選定時のsnapshot、配列順は優先度ではありません。"
      meta={run === null ? null : <div className="flex flex-wrap items-center justify-end gap-3">
        <UpdatedAtBadge value={run.generated_at} /><AsOfBadge compact value={runAsOf} />
        <div className="flex flex-col items-end gap-1">
          <TradingViewWatchlistExportButton filename={`candidates-${runAsOf}-tradingview.txt`} label="候補をTradingView用TXTに出力" tickers={candidates.map((entry) => entry.ticker)} />
          <a className="text-xs text-muted-foreground underline" href="https://www.tradingview.com/support/solutions/43000487233-how-to-import-or-export-a-watchlist/" rel="noopener noreferrer" target="_blank">TradingViewでの取り込み方法</a>
        </div>
      </div>}
      title="機械候補"
    >
      {method && <p className="px-5 py-2 text-xs text-muted-foreground">候補方式 {method.method.method_id} · {method.candidate_count}件 · ticker昇順は優先度を意味しません。</p>}
      {run === null ? <p className="px-5 py-6 text-sm text-muted-foreground">分析未作成。</p> : candidates.length === 0 ? <p className="px-5 py-6 text-sm text-muted-foreground">今回の機械候補は0件</p> : (
        <Table className="min-w-[1500px]">
          <TableHeader><TableRow>
            <TableHead>銘柄 / 名称</TableHead><TableHead>sector</TableHead><TableHead>評価法 / Approach内順位</TableHead>
            <TableHead className="text-right">PER (F/T)</TableHead><TableHead className="text-right">3FY平均EPSベースPER</TableHead><TableHead className="text-right">PBR</TableHead>
            <TableHead className="text-right">FCF yield</TableHead><TableHead className="text-right">Net cash / MC</TableHead><TableHead className="text-right">sales YoY</TableHead>
            <TableHead className="text-right">機械E[r] (secondary)</TableHead><TableHead>注記</TableHead><TableHead>選定時分析</TableHead>
          </TableRow></TableHeader>
          <TableBody>{candidates.map((entry) => (
            <TableRow key={entry.ticker}>
              <TableCell><Link className="font-mono font-semibold hover:underline" state={{ exploration: pathname + search }} to={`/securities/${entry.ticker}`}>{entry.ticker}</Link><span className="ml-2 text-muted-foreground">{entry.name}</span></TableCell>
              <TableCell>{entry.sector_33 ?? EMPTY}</TableCell>
              <TableCell><div className="flex flex-wrap gap-1">{entry.candidate.reasons.map((reason) => <Badge key={reason.valuation_approach_id} variant="secondary">{valuationApproachLabel(reason.valuation_approach_id, reason.rank)}</Badge>)}</div></TableCell>
              <TableCell className="text-right font-mono">{number(entry.per_forward)} / {number(entry.per_trailing)}</TableCell>
              <TableCell className="text-right font-mono">{number(entry.normalized_per_3fy)}</TableCell>
              <TableCell className="text-right font-mono">{number(entry.pbr)}</TableCell>
              <TableCell className="text-right font-mono">{percent(entry.fcf_yield)}</TableCell>
              <TableCell className="text-right font-mono">{percent(entry.net_cash_to_market_cap)}</TableCell>
              <TableCell className="text-right font-mono">{percent(entry.sales_yoy ?? null)}</TableCell>
              <TableCell className="text-right font-mono">{percent(entry.er_annual)}</TableCell>
              <TableCell>{entryNotes(entry)}</TableCell>
              <TableCell className="whitespace-normal"><details className="min-w-72"><summary className="cursor-pointer font-medium">選定時分析</summary><FrozenAnalysis analysis={entry} /></details></TableCell>
            </TableRow>
          ))}</TableBody>
        </Table>
      )}
    </SectionCard>
  )
}
