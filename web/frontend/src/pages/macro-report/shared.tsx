import type { MacroSeriesReferenceView } from '../../api/types'
import { ReportToneBadge } from '../../components/report/ReportToneBadge'
import { Badge } from '../../components/ui/badge'
import { labelMacroSection, labelMacroValue, macroTone } from '../../lib/macro-report'

export function SourceIds({ ids }: { ids: readonly string[] | null | undefined }) {
  if (!ids?.length) return null
  return <p className="mt-1 break-words font-mono text-[10px] leading-relaxed text-muted-foreground/75">出典: {ids.join(', ')}</p>
}

export function SeriesReferences({ series }: { series: readonly MacroSeriesReferenceView[] | null | undefined }) {
  if (!series?.length) return null
  return (
    <div className="flex flex-wrap gap-1.5">
      {series.map((item) => <Badge className="max-w-full whitespace-normal break-all text-left" key={item.series_id} variant="outline">{item.name} · {item.series_id}</Badge>)}
    </div>
  )
}

export function JudgmentBadge({ direction, confidence }: { direction: string; confidence: string }) {
  return <><ReportToneBadge tone={macroTone(direction)}>{labelMacroValue(direction)}</ReportToneBadge><span className="text-xs text-muted-foreground">確信度 {labelMacroValue(confidence)}</span></>
}

export function EvidenceDisclosure({ channels, ids, series }: { channels?: readonly string[]; ids?: readonly string[] | null; series?: readonly MacroSeriesReferenceView[] | null }) {
  const sourceCount = ids?.length ?? 0
  const seriesCount = series?.length ?? 0
  const channelCount = channels?.length ?? 0
  if (sourceCount + seriesCount + channelCount === 0) return null
  return (
    <details className="group border-t pt-3 text-xs text-muted-foreground">
      <summary className="w-fit cursor-pointer list-none rounded-sm font-medium outline-none marker:content-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 [&::-webkit-details-marker]:hidden">
        根拠 {sourceCount}件{seriesCount > 0 && `・指標 ${seriesCount}件`}{channelCount > 0 && `・経済チャネル ${channelCount}件`} <span aria-hidden="true" className="inline-block transition-transform group-open:rotate-90">›</span>
      </summary>
      <div className="mt-3 grid gap-2">
        {channelCount > 0 && <p>経済チャネル: {channels!.map(labelMacroSection).join(' / ')}</p>}
        <SeriesReferences series={series} />
        <SourceIds ids={ids} />
      </div>
    </details>
  )
}

export function Disclosure({ label, children }: { label: React.ReactNode; children: React.ReactNode }) {
  return (
    <details className="group rounded-lg border bg-card">
      <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-3 rounded-lg px-4 py-3 font-medium outline-none marker:content-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 [&::-webkit-details-marker]:hidden">
        <span>{label}</span>
        <span aria-hidden="true" className="text-muted-foreground transition-transform group-open:rotate-90">›</span>
      </summary>
      <div className="border-t px-4 py-4">{children}</div>
    </details>
  )
}
