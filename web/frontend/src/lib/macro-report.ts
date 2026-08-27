import type { MacroCoreSectionView, MacroScenarioView, MacroSynthesisView } from '../api/types'

export const TRANSMISSION_CHANNEL_IDS = [
  'rates_policy',
  'growth_demand',
  'inflation_costs',
  'liquidity_credit',
  'fx',
  'japan',
  'valuation',
] as const

export const INTEGRATED_SECTION_IDS = ['regime_summary', 'risk_environment', 'monitoring'] as const

export const SECTION_LABELS: Readonly<Record<string, string>> = {
  regime_summary: 'レジーム要約',
  rates_policy: '金利・金融政策',
  growth_demand: '景気・需要',
  inflation_costs: 'インフレ・コスト',
  liquidity_credit: '流動性・信用',
  fx: '為替',
  japan: '日本',
  valuation: 'バリュエーション',
  risk_environment: 'リスク環境',
  monitoring: '監視',
}

const VALUE_LABELS: Readonly<Record<string, string>> = {
  supportive: '支援的',
  adverse: '逆風',
  mixed: '混在',
  high: '高',
  medium: '中',
  low: '低',
  risk_seeking: 'リスク選好',
  neutral: '中立',
  risk_averse: 'リスク回避',
  base: '中心',
  bear: '下振れ',
  bull: '上振れ',
}

const SCENARIO_ORDER: Readonly<Record<string, number>> = { base: 0, bear: 1, bull: 2 }

export interface MacroCorePartition {
  channels: MacroCoreSectionView[]
  regime: MacroCoreSectionView | null
  risk: MacroCoreSectionView | null
  monitoring: MacroCoreSectionView | null
  other: MacroCoreSectionView[]
}

export function labelMacroValue(value: string): string {
  return VALUE_LABELS[value] ?? value
}

export function labelMacroSection(sectionId: string): string {
  return SECTION_LABELS[sectionId] ?? sectionId
}

export function partitionMacroCore(core: readonly MacroCoreSectionView[] | null | undefined): MacroCorePartition {
  const sections = core ?? []
  const byId = new Map(sections.map((section) => [section.section_id, section]))
  const known = new Set<string>([...TRANSMISSION_CHANNEL_IDS, ...INTEGRATED_SECTION_IDS])
  return {
    channels: TRANSMISSION_CHANNEL_IDS.flatMap((id) => {
      const section = byId.get(id)
      return section ? [section] : []
    }),
    regime: byId.get('regime_summary') ?? null,
    risk: byId.get('risk_environment') ?? null,
    monitoring: byId.get('monitoring') ?? null,
    other: sections.filter((section) => !known.has(section.section_id)),
  }
}

export function orderMacroScenarios(scenarios: readonly MacroScenarioView[] | null | undefined): MacroScenarioView[] {
  return [...(scenarios ?? [])].sort((left, right) => {
    const orderDelta = (SCENARIO_ORDER[left.case] ?? Number.MAX_SAFE_INTEGER) - (SCENARIO_ORDER[right.case] ?? Number.MAX_SAFE_INTEGER)
    return orderDelta || left.case.localeCompare(right.case)
  })
}

export function forceTitle(synthesis: MacroSynthesisView | null | undefined, forceId: string): string {
  return synthesis?.dominant_forces?.find((force) => force.force_id === forceId)?.title ?? forceId
}
