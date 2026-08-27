import type { MacroCoreSectionView } from '../../api/types'
import { SectionCard } from '../../components/SectionCard'
import { Badge } from '../../components/ui/badge'
import { labelMacroSection, labelMacroValue } from '../../lib/macro-report'
import type { MacroCorePartition } from '../../lib/macro-report'
import { Disclosure, JudgmentBadge, SeriesReferences, SourceIds } from './shared'

function CoreEvidence({ section, headingLevel = 3, judgmentShown = false, historyShown = false }: { section: MacroCoreSectionView; headingLevel?: 3 | 4; judgmentShown?: boolean; historyShown?: boolean }) {
  const Heading = headingLevel === 3 ? 'h3' : 'h4'
  return (
    <div className="grid gap-5">
      {!judgmentShown && <div className="grid gap-2"><div className="flex flex-wrap items-center gap-2"><Heading className="font-semibold">判断</Heading><JudgmentBadge confidence={section.judgment.confidence} direction={section.judgment.direction} /></div><p>{section.judgment.summary}</p><SourceIds ids={section.judgment.source_ids} /></div>}
      {!historyShown && (section.change_since_previous || section.previous_scorecard_review) && <div className="grid gap-2 border-t pt-4">{section.change_since_previous && <p><span className="font-medium">前回からの変化:</span> {section.change_since_previous}</p>}{section.previous_scorecard_review && <p><span className="font-medium">前回シナリオの確認:</span> {section.previous_scorecard_review}</p>}</div>}
      {(section.material_deltas ?? []).length > 0 && <div className="grid gap-3 border-t pt-4"><Heading className="font-semibold">重要な変化</Heading>{section.material_deltas.map((delta, index) => <article className="grid gap-2 rounded-lg bg-muted/45 p-3" key={`${delta.channel}-${index}`}><div className="flex flex-wrap gap-2"><Badge variant="outline">{delta.channel}</Badge><Badge variant="secondary">{labelMacroValue(delta.direction)} / 重要度 {labelMacroValue(delta.materiality)}</Badge></div><p>{delta.summary}</p><p className="text-xs text-muted-foreground">使用先: {delta.used_for}</p><SourceIds ids={delta.source_ids} /></article>)}</div>}
      <div className="grid gap-2 border-t pt-4"><Heading className="font-semibold">経済経路への接続</Heading><p>{section.economic_connection.summary}</p><SourceIds ids={section.economic_connection.source_ids} /></div>
      {(section.fact_summary ?? []).length > 0 && <div className="grid gap-3 border-t pt-4"><Heading className="font-semibold">事実</Heading>{section.fact_summary.map((fact, index) => <div key={`fact-${index}`}><p>{fact.summary}</p><SourceIds ids={fact.source_ids} /></div>)}</div>}
      {(section.series ?? []).length > 0 && <div className="grid gap-2 border-t pt-4"><Heading className="text-xs font-semibold text-muted-foreground">参照指標</Heading><SeriesReferences series={section.series} /></div>}
    </div>
  )
}

function DetailSummary({ section }: { section: MacroCoreSectionView }) {
  return <span className="flex flex-wrap items-center gap-2"><span>{labelMacroSection(section.section_id)}</span><span className="text-xs font-normal text-muted-foreground">{labelMacroValue(section.judgment.direction)} / 確信度 {labelMacroValue(section.judgment.confidence)}{section.change_since_previous ? ' · 前回から変化あり' : ''}</span></span>
}

export function MacroEvidenceDetails({ partition }: { partition: MacroCorePartition }) {
  const integrated = [partition.regime, partition.risk, partition.monitoring, ...partition.other].filter((section): section is MacroCoreSectionView => section !== null)
  return (
    <SectionCard description="事実・判断・経済経路への接続と参照指標 / source ID を、必要な経済チャネルだけ開いて確認できます。" padded title="詳細な経済チャネルと証拠">
      <div className="grid gap-3">
        {partition.channels.map((section) => <Disclosure key={section.section_id} label={<DetailSummary section={section} />}><CoreEvidence section={section} /></Disclosure>)}
        {integrated.length > 0 && <Disclosure label="統合判断の詳細"><div className="grid gap-6">{integrated.map((section) => <section className="grid gap-3 border-b pb-6 last:border-b-0 last:pb-0" key={section.section_id}><h3 className="font-semibold">{labelMacroSection(section.section_id)}</h3><CoreEvidence headingLevel={4} historyShown={section.section_id === 'regime_summary'} judgmentShown={section.section_id === 'regime_summary'} section={section} /></section>)}</div></Disclosure>}
      </div>
    </SectionCard>
  )
}
