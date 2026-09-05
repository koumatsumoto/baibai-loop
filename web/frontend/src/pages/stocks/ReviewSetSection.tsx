import { Link } from 'react-router'

import type { ReviewSetAnalysisView, ReviewSetEntryView, ReviewSetView } from '../../api/types'
import { AsOfBadge } from '../../components/AsOfBadge'
import { SectionCard } from '../../components/SectionCard'
import { UpdatedAtBadge } from '../../components/UpdatedAtBadge'
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

function entryNotes(entry: ReviewSetEntryView) {
  const quality = entry.analysis.data_quality
  const context = entry.analysis.context
  const flags = [
    quality.stale_fin_flag === true ? 'stale_fin' : null,
    quality.bs_carry_forward_fields,
    quality.edinet_failure_reasons,
    context.next_earnings_status,
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

function FrozenAnalysis({ analysis }: { analysis: ReviewSetAnalysisView }) {
  const identity = analysis.identity_liquidity
  const valuation = analysis.valuation
  const current = analysis.current_earnings
  const normalized = analysis.normalized_earnings
  const asset = analysis.asset_value
  const reinvestment = analysis.reinvestment
  const expected = analysis.expected_return
  const quality = analysis.data_quality
  const context = analysis.context
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
        ['normalized PER 3FY', number(normalized.normalized_per_3fy)], ['sector gap', number(normalized.normalized_per_3fy_sector_gap)],
      ]} title="Normalized earnings" />
      <FrozenGroup fields={[
        ['asset-backed ratio', percent(asset.asset_backed_ratio)], ['Net cash / MC', percent(asset.net_cash_to_market_cap)],
        ['investment securities', number(asset.investment_securities)], ['equity ratio', percent(asset.equity_ratio)],
      ]} title="Asset value" />
      <FrozenGroup fields={reinvestment === null ? [['status', 'not applicable']] : [
        ['P/S sector gap', number(reinvestment.p_s_sector_gap)], ['capital return proxy', percent(reinvestment.operating_return_on_capital_proxy)],
        ['sales YoY', percent(reinvestment.sales_yoy)], ['operating margin', percent(reinvestment.operating_margin)], ['FCF yield', percent(reinvestment.fcf_yield)],
      ]} title="Reinvestment" />
      <FrozenGroup fields={[
        ['machine E[r] annual', percent(expected.er_annual)], ['reversion', percent(expected.er_reversion_annual)], ['carry', percent(expected.er_carry_annual)],
        ['FV sector median', `${number(expected.fv_sector_median_yen, 0)} 円`], ['FV self range', `${number(expected.fv_self_range_yen, 0)} 円`],
        ['origin', expected.er_origin ?? EMPTY], ['model', expected.er_model_version ?? EMPTY], ['unit', expected.er_unit ?? EMPTY], ['assumptions', expected.er_assumptions ?? EMPTY],
      ]} title="Machine return prior (secondary context)" />
      <FrozenGroup fields={[
        ['BS carry fields', quality.bs_carry_forward_fields ?? EMPTY], ['BS carry lag', `${number(quality.bs_carry_forward_lag_days, 0)} 日`],
        ['EDINET failures', quality.edinet_failure_reasons ?? EMPTY], ['stale financials', String(quality.stale_fin_flag ?? EMPTY)],
      ]} title="Data quality" />
      <FrozenGroup fields={[
        ['next earnings status', context.next_earnings_status ?? EMPTY], ['next earnings', context.next_earnings_estimated_date ?? EMPTY],
        ['margin short / ADV', number(context.margin_short_to_adv)], ['TSE capital policy', context.tse_capital_policy_status ?? EMPTY],
        ['large holding filing', String(context.large_holding_filing_within_lookback ?? EMPTY)],
        ['tender offer filing', String(context.tender_offer_filing_within_lookback ?? EMPTY)],
      ]} title="Context" />
    </div>
  )
}

export function ReviewSetSection({ reviewSet, runAsOf }: { reviewSet: ReviewSetView | null; runAsOf: string }) {
  return (
    <SectionCard
      description="configured Valuation Approaches が生成した Nomination の exact union。配列順は優先度ではなく、表示値は選定時にfreezeされたsnapshot。"
      meta={reviewSet === null ? null : <div className="flex flex-wrap gap-3"><UpdatedAtBadge value={reviewSet.created_at} /><AsOfBadge compact value={runAsOf} /></div>}
      title="Review Set"
    >
      {reviewSet === null ? <p className="px-5 py-6 text-sm text-muted-foreground">Review Set はまだありません。</p> : (
        <Table className="min-w-[1500px]">
          <TableHeader><TableRow>
            <TableHead>銘柄 / 名称</TableHead><TableHead>sector</TableHead><TableHead>評価法 / Approach内順位</TableHead>
            <TableHead className="text-right">PER (F/T)</TableHead><TableHead className="text-right">Norm PER</TableHead><TableHead className="text-right">PBR</TableHead>
            <TableHead className="text-right">FCF yield</TableHead><TableHead className="text-right">Net cash / MC</TableHead><TableHead className="text-right">sales YoY</TableHead>
            <TableHead className="text-right">機械E[r] (secondary)</TableHead><TableHead>注記</TableHead><TableHead>選定時分析</TableHead>
          </TableRow></TableHeader>
          <TableBody>{reviewSet.entries.map((entry) => (
            <TableRow key={entry.ticker}>
              <TableCell><Link className="font-mono font-semibold hover:underline" to={`/securities/${entry.ticker}`}>{entry.ticker}</Link><span className="ml-2 text-muted-foreground">{entry.name}</span></TableCell>
              <TableCell>{entry.sector_33 ?? EMPTY}</TableCell>
              <TableCell><div className="flex flex-wrap gap-1">{entry.nominations.map((nomination) => <Badge key={nomination.valuation_approach_id} variant="secondary">{valuationApproachLabel(nomination.valuation_approach_id, nomination.rank)}</Badge>)}</div></TableCell>
              <TableCell className="text-right font-mono">{number(entry.analysis.valuation.per_forward)} / {number(entry.analysis.valuation.per_trailing)}</TableCell>
              <TableCell className="text-right font-mono">{number(entry.analysis.normalized_earnings.normalized_per_3fy)}</TableCell>
              <TableCell className="text-right font-mono">{number(entry.analysis.valuation.pbr)}</TableCell>
              <TableCell className="text-right font-mono">{percent(entry.analysis.current_earnings.fcf_yield)}</TableCell>
              <TableCell className="text-right font-mono">{percent(entry.analysis.asset_value.net_cash_to_market_cap)}</TableCell>
              <TableCell className="text-right font-mono">{percent(entry.analysis.reinvestment?.sales_yoy ?? null)}</TableCell>
              <TableCell className="text-right font-mono">{percent(entry.analysis.expected_return.er_annual)}</TableCell>
              <TableCell>{entryNotes(entry)}</TableCell>
              <TableCell className="whitespace-normal"><details className="min-w-72"><summary className="cursor-pointer font-medium">選定時分析</summary><FrozenAnalysis analysis={entry.analysis} /></details></TableCell>
            </TableRow>
          ))}</TableBody>
        </Table>
      )}
    </SectionCard>
  )
}
