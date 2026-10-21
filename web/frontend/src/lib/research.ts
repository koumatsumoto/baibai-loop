import { formatPct } from './format'

export function researchRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

export function researchText(value: unknown): string {
  return value == null ? '未確認' : String(value)
}

/** Display only: never round the stored valuation or purchase comparison. */
export function researchReturnPct(value: unknown): string {
  if (typeof value !== 'number' && (typeof value !== 'string' || value.trim() === '')) return '未確認'
  const number = Number(value)
  return Number.isFinite(number) ? formatPct(number, { digits: 4 }) : '未確認'
}

export const ACTION_LABELS: Readonly<Record<string, string>> = {
  buy: '購入の提案', hold: '継続保有の提案', sell: '売却の提案', no_action: '見送り', defer: '判断保留',
}

export const STANCE_LABELS: Readonly<Record<string, string>> = {
  supported: '根拠あり', uncertain: '未確定', unsupported: '支持できない',
}

const WARNING_LABELS: Readonly<Record<string, string>> = {
  budget_guide_under: '提案金額が予算の目安下限を下回ります',
  budget_guide_exceeded: '提案金額が予算の目安上限を超えます',
  adv_participation_unassessed: '平均売買代金（ADV）が未確認です',
  adv_participation_exceeds_warning: '平均売買代金に対する提案比率が警告水準を超えます',
  portfolio_exposure_common_factor_coverage_incomplete: '共通要因の分類が未確認の保有・予約・対象があります',
  concentration_and_dry_powder_unassessed: '集中度と現金余力を評価できません',
  dry_powder_below_floor: '買付後の現金余力が警告水準を下回ります',
}

export function proposalWarningLabel(code: string): string {
  if (WARNING_LABELS[code]) return WARNING_LABELS[code]
  for (const [prefix, label] of [
    ['portfolio_valuation_unknown:', '保有時価を評価できません'],
    ['prospective_ticker_concentration_exceeds_warning:', '買付後の銘柄集中度が警告水準を超えます'],
    ['prospective_sector_concentration_exceeds_warning:', '買付後の業種集中度が警告水準を超えます'],
    ['prospective_common_factor_concentration_exceeds_warning:', '買付後の共通要因集中度が警告水準を超えます'],
  ]) if (code.startsWith(prefix)) return `${label}（${code.slice(prefix.length)}）`
  return code
}

export const RETURN_UNAVAILABLE_LABELS: Readonly<Record<string, string>> = {
  valuation_basis_requires_refresh: '期間と今後分配を当日起点で再確認する必要があります',
  valuation_unresolved: '価値見積りが未確定です',
  quote_missing_or_stale: '必要な時点の価格が未確認、または鮮度不足です',
  quantity_basis_unconfirmed: '保有数量の株式単位を確認する必要があります',
  corporate_action_unresolved: '株式単位と価格の整合を確認する必要があります',
}
