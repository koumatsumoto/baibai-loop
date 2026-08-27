import { CircleAlert } from 'lucide-react'

import type { MacroConnectionSectionView } from '../../api/types'
import { SectionCard } from '../../components/SectionCard'
import { Alert, AlertDescription, AlertTitle } from '../../components/ui/alert'
import { Badge } from '../../components/ui/badge'
import { labelMacroSection, labelMacroValue } from '../../lib/macro-report'
import { Disclosure, JudgmentBadge, SeriesReferences, SourceIds } from './shared'

export function MacroResearchImplications({ connection }: { connection: MacroConnectionSectionView }) {
  const caveats = connection.estimate_caveats ?? []
  const hints = connection.research_priority_hints ?? []
  const cautions = connection.sizing_cautions ?? []
  const tilts = connection.sector_tilts ?? []
  const hasEvidence = (connection.fact_summary?.length ?? 0) > 0 || (connection.series?.length ?? 0) > 0 || (connection.core_section_ids?.length ?? 0) > 0
  return (
    <SectionCard description="このsectionはresearchと見積りの注意点を示します。機械ranking、売買時期、投入額を自動決定しません。" padded title="日本株の調査・見積りへの含意">
      <div className="grid gap-5">
        <div className="grid gap-2 rounded-lg border p-4">
          <div className="flex flex-wrap items-center gap-2"><h3 className="font-semibold">Implication</h3><JudgmentBadge confidence={connection.judgment.confidence} direction={connection.judgment.direction} /></div>
          <p>{connection.judgment.summary}</p>
          <SourceIds ids={connection.judgment.source_ids} />
        </div>

        {caveats.length > 0 && <div className="grid gap-3"><h3 className="font-semibold">見積りへの注意</h3><div className="grid gap-3 lg:grid-cols-2">{caveats.map((caveat, index) => (
          <article className="grid gap-2 rounded-lg bg-muted/45 p-4" key={`caveat-${index}`}>
            <div className="flex flex-wrap gap-2"><Badge variant="outline">{caveat.affected_component}</Badge><Badge variant="secondary">重要度 {labelMacroValue(caveat.materiality)}</Badge><Badge variant="outline">対象: {caveat.applies_to}</Badge></div>
            <p>{caveat.summary}</p><SourceIds ids={caveat.source_ids} />
          </article>
        ))}</div></div>}

        {hints.length > 0 && <div className="grid gap-3"><h3 className="font-semibold">Research 優先度ヒント</h3>{hints.map((hint, index) => <div className="rounded-lg border p-4" key={`hint-${index}`}><Badge variant="outline">対象: {hint.applies_to}</Badge><p className="mt-2">{hint.summary}</p><SourceIds ids={hint.source_ids} /></div>)}</div>}

        {connection.bargain_topography && <div className="grid gap-2 rounded-lg border p-4"><h3 className="font-semibold">バーゲン地形</h3><p>{connection.bargain_topography.summary}</p><SourceIds ids={connection.bargain_topography.source_ids} /></div>}

        {cautions.length > 0 && <div className="grid gap-3"><h3 className="font-semibold">投入判断への注意</h3>{cautions.map((caution, index) => <Alert key={`caution-${index}`} role="note"><CircleAlert /><AlertTitle>重要度 {labelMacroValue(caution.severity)}</AlertTitle><AlertDescription>{caution.summary}<SourceIds ids={caution.source_ids} /></AlertDescription></Alert>)}</div>}

        {tilts.length > 0 && <div className="grid gap-3 border-t pt-5"><h3 className="text-sm font-semibold">Sector tilt（参考仮説）</h3><div className="grid gap-3 lg:grid-cols-3">{tilts.map((tilt, index) => <article className="rounded-lg border p-4" key={`tilt-${index}`}><div className="flex flex-wrap gap-2"><Badge variant="outline">{tilt.sector}</Badge><Badge variant="secondary">{labelMacroValue(tilt.direction)}</Badge></div><p className="mt-2 text-sm">{tilt.summary}</p><SourceIds ids={tilt.source_ids} /></article>)}</div></div>}

        {hasEvidence && <Disclosure label="この含意の根拠"><div className="grid gap-5">
          {(connection.fact_summary ?? []).length > 0 && <div className="grid gap-3"><h3 className="font-semibold">Fact</h3>{connection.fact_summary.map((fact, index) => <div key={`fact-${index}`}><p>{fact.summary}</p><SourceIds ids={fact.source_ids} /></div>)}</div>}
          {((connection.core_section_ids?.length ?? 0) > 0 || (connection.series?.length ?? 0) > 0) && <div className="grid gap-3 border-t pt-4"><h3 className="font-semibold">参照</h3>
            {(connection.core_section_ids?.length ?? 0) > 0 && <p className="text-sm"><span className="font-medium">依拠する経済チャネル:</span> {connection.core_section_ids.map(labelMacroSection).join(' / ')}</p>}
            <SeriesReferences series={connection.series} />
          </div>}
        </div></Disclosure>}
      </div>
    </SectionCard>
  )
}
