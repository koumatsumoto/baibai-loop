import { Link } from 'react-router'
import { CartesianGrid, Line, LineChart, XAxis, YAxis } from 'recharts'
import type { MarketSnapshotView, SecurityMarketContextView } from '../../api/types'
import { SectionCard } from '../../components/SectionCard'
import { ChartContainer, ChartLegend, ChartLegendContent, ChartTooltip, ChartTooltipContent } from '../../components/ui/chart'
import { formatNumber, formatPct } from '../../lib/format'

function pct(value: number | null): string { return value === null ? '未算出' : formatPct(value, { fraction: true }) }
function gap(value: number | null): string { return value === null ? '未算出' : `${value > 0 ? '+' : ''}${formatNumber(value * 100, 1)}pt` }
function Metric({ label, value }: { label: string; value: string }) {
  return <div><dt className="text-xs text-muted-foreground">{label}</dt><dd className="mt-1 font-mono text-sm tabular-nums">{value}</dd></div>
}

export function MarketSnapshotSection({ snapshot }: { snapshot: MarketSnapshotView | null }) {
  if (!snapshot) return <SectionCard padded title="市場・業種の動き"><p className="text-sm text-muted-foreground">保存済みの市場・業種の観測はありません。</p></SectionCard>
  const latest = snapshot.points.at(-1)
  const hasHistory = snapshot.points.some(point => point.benchmark_return_20d !== null || point.benchmark_return_60d !== null)
  const sectors = snapshot.sectors.map(row => <li className="grid gap-2 border-t py-3 sm:grid-cols-[minmax(12rem,2fr)_3fr] sm:items-center" key={row.sector_33}>
    <div><Link className="text-sm font-medium underline-offset-4 hover:underline" to={`/stocks/explore?${new URLSearchParams({ sector: row.sector_33 })}`}>{row.sector_33} →</Link><p className="mt-1 text-xs text-muted-foreground">20日騰落の対象 {row.ticker_count}銘柄</p></div>
    <dl className="grid grid-cols-3 gap-3"><Metric label="20日中央値" value={pct(row.median_return_20d)} /><Metric label="60日中央値" value={pct(row.median_return_60d)} /><Metric label="20日MA上回る割合" value={pct(row.pct_above_ma20)} /></dl>
  </li>)
  return <SectionCard padded title="市場・業種の動き" description={`読取の要求日 ${snapshot.as_of} · 比較対象の指数連動ETF ${snapshot.benchmark_ticker}`}>
    <div className="grid gap-5">
      {latest ? <><dl className="grid grid-cols-2 gap-4 sm:grid-cols-3"><Metric label="指数の20日価格騰落" value={pct(latest.benchmark_return_20d)} /><Metric label="指数の60日価格騰落" value={pct(latest.benchmark_return_60d)} /><Metric label="20日移動平均を上回る割合" value={pct(latest.breadth_pct_above_ma20)} /></dl>
        <p className="text-xs text-muted-foreground">指数の観測日 {latest.observation_date ?? '未観測'} · 広がりの集計対象 {latest.breadth_sample_size}銘柄</p>
        {hasHistory ? <div><p className="mb-2 text-xs font-medium text-muted-foreground">指数価格騰落の週次推移</p><ChartContainer className="h-44 w-full" config={{ benchmark_return_20d: { label: '20日価格騰落', color: 'var(--chart-1)' }, benchmark_return_60d: { label: '60日価格騰落', color: 'var(--chart-2)' } }}>
          <LineChart data={snapshot.points} margin={{ left: 0, right: 16 }}><CartesianGrid vertical={false} /><XAxis axisLine={false} dataKey="date" minTickGap={30} tickLine={false} tickFormatter={value => String(value).slice(5)} /><YAxis axisLine={false} tickLine={false} tickFormatter={value => pct(Number(value))} width={52} /><ChartTooltip content={<ChartTooltipContent formatter={(value, name) => <span>{name === 'benchmark_return_20d' ? '20日' : '60日'} {pct(Number(value))}</span>} />} /><ChartLegend content={<ChartLegendContent />} /><Line connectNulls={false} dataKey="benchmark_return_20d" dot={false} isAnimationActive={false} stroke="var(--chart-1)" /><Line connectNulls={false} dataKey="benchmark_return_60d" dot={false} isAnimationActive={false} stroke="var(--chart-2)" /></LineChart>
        </ChartContainer></div> : <p className="text-sm text-muted-foreground">指数騰落の推移は未算出です。</p>}</> : <p className="text-sm text-muted-foreground">指数の週次観測はありません。</p>}
      <div><h3 className="mb-2 text-sm font-medium">業種別の価格騰落・銘柄探索</h3><ul>{sectors.slice(0, 6)}</ul>{sectors.length > 6 && <details><summary className="cursor-pointer py-2 text-sm">他の業種を見る（{sectors.length - 6}業種）</summary><ul>{sectors.slice(6)}</ul></details>}{sectors.length === 0 && <p className="text-sm text-muted-foreground">業種別の集計値はありません。</p>}</div>
      <p className="text-xs text-muted-foreground">配当を含まない価格騰落です。業種中央値は利用可能な構成銘柄の集計で、個別の自己除外peer比較とは母集団が異なります。銘柄・業種情報の基準 {snapshot.master_snapshot_date ?? '未記録'}。</p>
    </div>
  </SectionCard>
}

export function SecurityMarketContextSection({ context }: { context: SecurityMarketContextView | null }) {
  if (!context) return <SectionCard padded title="株価と市場・同業との比較"><p className="text-sm text-muted-foreground">保存済みの価格・比較情報はありません。</p></SectionCard>
  const { price, relative } = context
  const peer = relative?.sector
  return <SectionCard padded title="株価と市場・同業との比較" description={`読取の要求日 ${context.as_of}`}>
    <div className="grid gap-5">
      {price ? <><p className="text-sm">観測日 {price.resolved_date} · 価格調整済みの参考終値 <span className="font-mono">{formatNumber(price.close, 2)}円</span></p>
        <p className="text-xs text-muted-foreground">未調整終値に独立した価格調整係数を適用した価格騰落で、配当を含みません。Stock Assessment・Pmax・注文の未調整価格とは基準が異なります。</p>
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4"><Metric label="1日価格騰落" value={pct(price.return_1d)} /><Metric label="5日価格騰落" value={pct(price.return_5d)} /><Metric label="20日価格騰落" value={pct(price.return_20d)} /><Metric label="60日価格騰落" value={pct(price.return_60d)} /></dl>
        <details><summary className="cursor-pointer text-sm">自己範囲・変動・売買代金</summary><dl className="mt-3 grid grid-cols-2 gap-4 sm:grid-cols-3"><Metric label={`過去${price.self_range_window_bars}本内の価格順位`} value={pct(price.self_range_percentile)} /><Metric label="52週安値からの乖離" value={pct(price.gap_from_52w_low)} /><Metric label="52週高値からの乖離" value={pct(price.gap_from_52w_high)} /><Metric label="20日実現変動率（年率）" value={pct(price.realized_vol_20d)} /><Metric label="20日平均売買代金" value={price.avg_turnover_20d_oku === null ? '未算出' : `${formatNumber(price.avg_turnover_20d_oku, 2)}億円`} /></dl><p className="mt-3 text-xs text-muted-foreground">株価basis {price.price_basis} · 保存した価格 {price.bar_count}本</p></details>
      </> : <p className="text-sm text-muted-foreground">価格の保存観測はありません。</p>}
      {relative ? <div><h3 className="text-sm font-medium">指数連動ETF {relative.benchmark_ticker} との価格騰落差</h3><dl className="mt-3 grid grid-cols-2 gap-4 sm:grid-cols-4"><Metric label="1日差" value={gap(relative.relative_1d)} /><Metric label="5日差" value={gap(relative.relative_5d)} /><Metric label="20日差" value={gap(relative.relative_20d)} /><Metric label="60日差" value={gap(relative.relative_60d)} /></dl>{context.benchmark_trend && <p className="mt-3 text-xs text-muted-foreground">指数単体の観測日 {context.benchmark_trend.observation_date}</p>}</div> : <p className="text-sm text-muted-foreground">指数との比較値はありません。</p>}
      {peer ? <div><h3 className="text-sm font-medium">{peer.sector_33} · 自己除外peer比較</h3><dl className="mt-3 grid grid-cols-2 gap-4"><Metric label="同業の20日価格騰落中央値" value={pct(peer.peer_median_return_20d)} /><Metric label="同業中央値との差" value={gap(peer.relative_return_20d)} /></dl><p className="mt-3 text-xs text-muted-foreground">対象銘柄を除いた {peer.peer_count}銘柄 · 同じ端点 {peer.comparison_start_date} → {peer.comparison_end_date} · 銘柄・業種情報 {peer.master_snapshot_date ?? '未記録'}</p></div> : <p className="text-sm text-muted-foreground">同業との比較値はありません。</p>}
    </div>
  </SectionCard>
}
