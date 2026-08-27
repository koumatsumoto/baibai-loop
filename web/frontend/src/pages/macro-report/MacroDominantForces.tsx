import type { MacroSynthesisView } from '../../api/types'
import { CountercaseBlock } from '../../components/report/CountercaseBlock'
import { SectionCard } from '../../components/SectionCard'
import { forceTitle } from '../../lib/macro-report'
import { EvidenceDisclosure, JudgmentBadge } from './shared'

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
              <CountercaseBlock label="この見方への反証">{force.counter_evidence}</CountercaseBlock>
              <EvidenceDisclosure channels={force.core_section_ids} ids={force.source_ids} series={force.series} />
            </article>
          ))}
        </div>
        {interactions.map((interaction, index) => (
          <div className="grid gap-2 rounded-lg bg-muted/45 p-4" key={`interaction-${index}`}>
            <h3 className="font-semibold">力の相互作用 · {interaction.force_ids.map((id) => forceTitle(synthesis, id)).join(' × ')}</h3>
            <p>{interaction.summary}</p><EvidenceDisclosure ids={interaction.source_ids} />
          </div>
        ))}
      </div>
    </SectionCard>
  )
}
