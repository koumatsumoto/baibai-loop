import { CircleAlert } from 'lucide-react'

import type { MacroSynthesisView } from '../../api/types'
import { SectionCard } from '../../components/SectionCard'
import { Alert, AlertDescription, AlertTitle } from '../../components/ui/alert'
import { Badge } from '../../components/ui/badge'
import { forceTitle, labelMacroSection } from '../../lib/macro-report'
import { JudgmentBadge, SeriesReferences, SourceIds } from './shared'

export function MacroDominantForces({ synthesis }: { synthesis: MacroSynthesisView }) {
  const forces = synthesis.dominant_forces ?? []
  const interactions = synthesis.interactions ?? []
  return (
    <SectionCard description="複数の経済チャネルを横断して現局面を動かす力。反証も同じ面で確認します。" padded title="支配的な力">
      <div className="grid gap-4">
        <div className="grid gap-4 lg:grid-cols-2">
          {forces.map((force) => (
            <article className="grid min-w-0 content-start gap-3 rounded-lg border p-4" key={force.force_id}>
              <div className="flex flex-wrap items-center gap-2"><h3 className="text-base font-semibold">{force.title}</h3><JudgmentBadge confidence={force.confidence} direction={force.direction} /></div>
              <p>{force.summary}</p>
              <p className="text-sm"><span className="font-medium">伝達経路:</span> {force.transmission}</p>
              <Alert role="note"><CircleAlert /><AlertTitle>この見方への反証</AlertTitle><AlertDescription>{force.counter_evidence}</AlertDescription></Alert>
              <div className="grid gap-2 border-t pt-3 text-xs text-muted-foreground">
                <div className="flex flex-wrap gap-1.5">{force.core_section_ids.map((id) => <Badge key={id} variant="outline">{labelMacroSection(id)}</Badge>)}</div>
                <SeriesReferences series={force.series} />
                <SourceIds ids={force.source_ids} />
              </div>
            </article>
          ))}
        </div>
        {interactions.map((interaction, index) => (
          <Alert key={`interaction-${index}`} role="note">
            <CircleAlert />
            <AlertTitle>相互作用 · {interaction.force_ids.map((id) => forceTitle(synthesis, id)).join(' × ')}</AlertTitle>
            <AlertDescription>{interaction.summary}<SourceIds ids={interaction.source_ids} /></AlertDescription>
          </Alert>
        ))}
      </div>
    </SectionCard>
  )
}
