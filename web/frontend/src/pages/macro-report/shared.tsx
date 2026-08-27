import type { MacroSeriesReferenceView } from '../../api/types'
import { Badge } from '../../components/ui/badge'
import { labelMacroValue } from '../../lib/macro-report'

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
  return <Badge variant="secondary">{labelMacroValue(direction)} / 確信度 {labelMacroValue(confidence)}</Badge>
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
