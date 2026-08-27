import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { MacroContextView, MacroCoreSectionView, MacroScenarioView } from '../src/api/types'
import { forceTitle, labelMacroValue, macroTone, orderMacroScenarios, partitionMacroCore, scenarioDistribution, TRANSMISSION_CHANNEL_IDS } from '../src/lib/macro-report'
import { MacroReportContent } from '../src/pages/macro-report/MacroReportContent'
import { ScenarioDistribution } from '../src/pages/macro-report/ScenarioDistribution'

const CORE_IDS = ['regime_summary', ...TRANSMISSION_CHANNEL_IDS, 'risk_environment', 'monitoring']

function coreSection(section_id: string): MacroCoreSectionView {
  return {
    section_id,
    series: [{ series_id: `series.${section_id}`, name: `Series ${section_id}` }],
    fact_summary: [{ summary: `fact ${section_id}`, source_ids: [`source.fact.${section_id}`] }],
    judgment: { summary: `judgment ${section_id}`, direction: 'mixed', confidence: 'medium', source_ids: [`source.judgment.${section_id}`] },
    economic_connection: { summary: `connection ${section_id}`, source_ids: [`source.connection.${section_id}`] },
    change_since_previous: section_id === 'regime_summary' ? 'regime changed' : null,
    previous_scorecard_review: section_id === 'regime_summary' ? 'scorecard reviewed' : null,
    material_deltas: [],
    risk_environment: section_id === 'risk_environment' ? { stance: 'neutral', confidence: 'medium', summary: 'risk summary', falsifiers: ['risk falsifier visible'], source_ids: ['source.risk'] } : null,
    scenarios: section_id === 'risk_environment' ? [scenario('bull'), scenario('base'), scenario('bear')] : [],
    monitoring_points: section_id === 'monitoring' ? [{ event: 'policy meeting', condition: 'rate changes', view_change: 'reassess duration', summary: 'watch policy', source_ids: ['source.monitor'] }] : [],
  }
}

function scenario(caseName: string): MacroScenarioView {
  const probabilities: Readonly<Record<string, number>> = { base: 0.5, bear: 0.3, bull: 0.2 }
  return {
    case: caseName,
    direction: 'mixed',
    probability: probabilities[caseName] ?? null,
    summary: `${caseName} scenario`,
    conditions: [`${caseName} condition`],
    scorecard: [{ series_id: `score.${caseName}`, comparison: 'above', threshold: 1, deadline: '2026-12-31' }],
    economic_implications: [`${caseName} implication`],
    source_ids: [`source.scenario.${caseName}`],
  }
}

function report(): MacroContextView {
  return {
    context_id: 'macro-context-test',
    as_of: '2026-08-26',
    published_at: '2026-08-27T00:00:00Z',
    summary: 'current macro summary',
    age_days: 1,
    stale: false,
    synthesis: {
      dominant_forces: [{
        force_id: 'force.one',
        title: 'Force One',
        summary: 'force summary',
        transmission: 'force transmission',
        core_section_ids: ['rates_policy'],
        series: [{ series_id: 'series.force', name: 'Force series' }],
        counter_evidence: 'counter evidence visible',
        direction: 'mixed',
        confidence: 'high',
        source_ids: ['source.force'],
      }],
      interactions: [{ summary: 'interaction summary', force_ids: ['force.one', 'force.unknown'], source_ids: ['source.interaction'] }],
    },
    core: CORE_IDS.map(coreSection),
    connection: {
      section_id: 'connection',
      series: [{ series_id: 'series.connection', name: 'Connection series' }],
      core_section_ids: ['rates_policy'],
      fact_summary: [{ summary: 'connection fact', source_ids: ['source.connection.fact'] }],
      judgment: { summary: 'research implication', direction: 'mixed', confidence: 'medium', source_ids: ['source.connection.judgment'] },
      research_priority_hints: [{ summary: 'research hint', applies_to: 'exporters', source_ids: ['source.hint'] }],
      sector_tilts: [{ sector: 'industrials', direction: 'mixed', summary: 'sector hypothesis', source_ids: ['source.tilt'] }],
      sizing_cautions: [{ severity: 'high', summary: 'sizing caution', source_ids: ['source.sizing'] }],
      bargain_topography: { summary: 'bargain topography', source_ids: ['source.topography'] },
      estimate_caveats: [{ summary: 'estimate caveat visible', applies_to: 'cyclicals', affected_component: 'FV', materiality: 'high', source_ids: ['source.caveat'] }],
    },
  }
}

describe('macro report projection', () => {
  it('partitions every core section exactly once', () => {
    const partition = partitionMacroCore(CORE_IDS.map(coreSection))
    const projected = [...partition.channels, partition.regime, partition.risk, partition.monitoring, ...partition.other].filter(Boolean)
    expect(projected.map((section) => section?.section_id)).toHaveLength(CORE_IDS.length)
    expect(new Set(projected.map((section) => section?.section_id))).toEqual(new Set(CORE_IDS))
  })

  it('keeps only the seven transmission channels in canonical order', () => {
    const partition = partitionMacroCore([...CORE_IDS].reverse().map(coreSection))
    expect(partition.channels.map((section) => section.section_id)).toEqual(TRANSMISSION_CHANNEL_IDS)
    expect(partition.channels.map((section) => section.section_id)).not.toContain('regime_summary')
    expect(partition.channels.map((section) => section.section_id)).not.toContain('risk_environment')
    expect(partition.channels.map((section) => section.section_id)).not.toContain('monitoring')
  })

  it('orders scenarios and falls back to raw unknown enum values', () => {
    expect(orderMacroScenarios([scenario('bull'), scenario('stress'), scenario('base'), scenario('bear')]).map((item) => item.case)).toEqual(['base', 'bear', 'bull', 'stress'])
    expect(labelMacroValue('supportive')).toBe('支援的')
    expect(labelMacroValue('new_value')).toBe('new_value')
  })

  it('maps macro judgment and scenario values without destructive tones', () => {
    expect(macroTone('supportive')).toBe('positive')
    expect(macroTone('adverse')).toBe('warning')
    expect(macroTone('mixed')).toBe('muted')
    expect(macroTone('bull')).toBe('positive')
    expect(macroTone('bear')).toBe('warning')
    expect(macroTone('base')).toBe('muted')
    expect(['supportive', 'adverse', 'mixed', 'risk_seeking', 'risk_averse', 'neutral', 'base', 'bear', 'bull'].map(macroTone)).not.toContain('destructive')
  })

  it('accepts only one valid base, bear and bull distribution without normalization', () => {
    const valid = [scenario('bull'), scenario('base'), scenario('bear')]
    expect(scenarioDistribution(valid)?.map((item) => [item.scenario.case, item.probability])).toEqual([
      ['base', 0.5], ['bear', 0.3], ['bull', 0.2],
    ])
    expect(scenarioDistribution(valid.slice(0, 2))).toBeNull()
    expect(scenarioDistribution([...valid, scenario('stress')])).toBeNull()
    expect(scenarioDistribution([...valid, scenario('base')])).toBeNull()
    for (const invalid of [null, Number.NaN, Number.POSITIVE_INFINITY, 0, 1]) {
      expect(scenarioDistribution(valid.map((item) => item.case === 'base' ? { ...item, probability: invalid } : item))).toBeNull()
    }
    expect(scenarioDistribution(valid.map((item) => item.case === 'base' ? { ...item, probability: 0.4 } : item))).toBeNull()
  })

  it('renders an aria-hidden 50/30/20 bar with a textual legend', () => {
    const markup = renderToStaticMarkup(createElement(ScenarioDistribution, { scenarios: [scenario('bull'), scenario('base'), scenario('bear')] }))
    expect(markup).toContain('見通しの分布（主観ウェイト）')
    expect(markup).toContain('aria-hidden="true"')
    expect(markup).toContain('width:50%')
    expect(markup).toContain('width:30%')
    expect(markup).toContain('width:20%')
    expect(markup).toContain('中心')
    expect(markup).toContain('下振れ')
    expect(markup).toContain('上振れ')
    expect(renderToStaticMarkup(createElement(ScenarioDistribution, { scenarios: [scenario('base')] }))).toBe('')
  })

  it('handles absent optional collections and synthesis', () => {
    expect(partitionMacroCore(undefined).channels).toEqual([])
    expect(orderMacroScenarios(undefined)).toEqual([])
    expect(forceTitle(null, 'force.raw')).toBe('force.raw')
    const legacy = { ...report(), synthesis: null, core: [], connection: undefined } as unknown as MacroContextView
    expect(() => renderToStaticMarkup(createElement(MacroReportContent, { data: legacy }))).not.toThrow()
  })

  it('resolves known force ids to titles and preserves unknown ids', () => {
    const synthesis = report().synthesis
    expect(forceTitle(synthesis, 'force.one')).toBe('Force One')
    expect(forceTitle(synthesis, 'force.unknown')).toBe('force.unknown')
  })

  it('does not strip source or series ids during projection', () => {
    const input = CORE_IDS.map(coreSection)
    const partition = partitionMacroCore(input)
    expect(partition.channels[0].series[0].series_id).toBe(input[1].series[0].series_id)
    expect(partition.channels[0].fact_summary[0].source_ids[0]).toBe(input[1].fact_summary[0].source_ids[0])
  })
})

describe('macro report render contract', () => {
  it('renders the judgment-first order and keeps rebuttal, lineage and scorecard evidence reachable', () => {
    const markup = renderToStaticMarkup(createElement(MacroReportContent, { data: report() }))
    const headings = ['現局面', '支配的な力', '日本株の調査・見積りへの含意', 'シナリオと見方を変える条件', '詳細な経済チャネルと証拠']
    for (let index = 1; index < headings.length; index += 1) {
      expect(markup.indexOf(headings[index - 1])).toBeLessThan(markup.indexOf(headings[index]))
    }
    expect(markup).toContain('counter evidence visible')
    expect(markup).toContain('risk falsifier visible')
    expect(markup).toContain('estimate caveat visible')
    expect(markup).toContain('事実')
    expect(markup).toContain('現局面のリスク環境と共通の出典')
    expect(markup).toContain('series.rates_policy')
    expect(markup).toContain('source.fact.rates_policy')
    expect(markup).toContain('score.base')
    expect(markup).toContain('Force One × force.unknown')
    expect(markup.match(/source\.risk/g)).toHaveLength(1)
    expect(markup).toContain('<h3 class="font-semibold">判断</h3>')
    expect(markup).toContain('<h4 class="font-semibold">判断</h4>')
    expect(markup).toContain('見通しの分布（主観ウェイト）')
    expect(markup.indexOf('current macro summary')).toBeLessThan(markup.indexOf('regime changed'))
    expect(markup.indexOf('regime changed')).toBeLessThan(markup.indexOf('中心像'))
    expect(markup.indexOf('見通しの分布（主観ウェイト）')).toBeLessThan(markup.indexOf('scorecard reviewed'))
    expect(markup).toContain('見積り・投入のリスク')
    expect(markup.indexOf('見積り・投入のリスク')).toBeLessThan(markup.indexOf('機会の地形・調査焦点'))
    expect(markup).toContain('観測すること')
    expect(markup).toContain('成立条件')
    expect(markup).toContain('見方の変更')
  })

  it('keeps risk-environment lineage when an old revision has no falsifiers', () => {
    const data = report()
    const risk = data.core.find((section) => section.section_id === 'risk_environment')
    if (risk?.risk_environment) risk.risk_environment.falsifiers = []
    const markup = renderToStaticMarkup(createElement(MacroReportContent, { data }))
    expect(markup).toContain('明示された反証条件はありません。')
    expect(markup.match(/source\.risk/g)).toHaveLength(1)
  })
})
