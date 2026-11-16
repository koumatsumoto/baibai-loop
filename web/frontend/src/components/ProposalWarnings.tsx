const DESCRIPTIONS = new Map([
  ['budget_guide_under', '買付金額が目安を下回ります。目安まで増やす指示ではありません。'],
  ['budget_guide_exceeded', '買付金額が目安を上回ります。資金不足の判定とは別です。'],
  ['adv_participation_exceeds_warning', '売買代金に対する提案規模が、保存時の注意水準を超えています。板の厚みや約定可能量を示しません。'],
  ['adv_participation_unassessed', '売買代金に対する提案規模は未評価です。比較に必要な入力が不足しており、注意がない・小さいとは判定していません。'],
  ['dry_powder_below_floor', 'この買付後の未拘束現金が、保存時の注意水準を下回ります。'],
  ['concentration_and_dry_powder_unassessed', '集中度と現金余力の診断は未評価です。診断に必要な保有時価または正の総資本を確認できません。'],
  ['portfolio_exposure_common_factor_coverage_incomplete', '共通要因が未分類の銘柄があります。見えている集中度だけで全体を評価できません。'],
])
const SCOPED_DESCRIPTIONS = [
  ['hold_active_reservation_requires_confirmation:', '保存入力の台帳に、保有継続を提案した銘柄の未完了予約が残っています。保有判断は既存注文の取消・失効確認ではありません。現在の注文状態は別に確認してください。'],
  ['prospective_ticker_concentration_exceeds_warning:', 'この提案を加えた銘柄別の集中が、保存時の注意水準を超えています。'],
  ['prospective_sector_concentration_exceeds_warning:', 'この提案を加えた業種別の集中が、保存時の注意水準を超えています。'],
  ['prospective_common_factor_concentration_exceeds_warning:', 'この提案を加えた共通要因への集中が、保存時の注意水準を超えています。同時損失の確率ではありません。'],
  ['portfolio_valuation_unknown:', '保存時に時価を確認できない保有銘柄があります。'],
] as const

function description(warning: string): string | null {
  const exact = DESCRIPTIONS.get(warning)
  if (exact !== undefined) return exact
  for (const [prefix, message] of SCOPED_DESCRIPTIONS) {
    if (warning.startsWith(prefix) && warning.length > prefix.length) {
      return `${message} 対象：${warning.slice(prefix.length)}`
    }
  }
  return null
}

export function ProposalWarnings({ title, warnings }: { title: string; warnings: string[] }) {
  if (warnings.length === 0) return null
  // 保存時の判定を説明するだけ。現在の閾値で再計算せず、未知codeや作者文も残す。
  const rows = warnings.map(warning => ({ warning, explanation: description(warning) }))
  return <div className="grid gap-2">
    <p className="text-sm font-medium">{title}</p>
    {rows.some(row => row.explanation !== null) && <p className="text-xs leading-6 text-muted-foreground">保存時の注意の説明です。現在の注文可否や、自動的な売買・数量変更の指示ではありません。</p>}
    <ul className="grid list-disc gap-3 pl-5 text-sm leading-7">
      {rows.map(({ warning, explanation }, index) => <li className="min-w-0" key={index}>
        {explanation === null
          ? <p className="whitespace-pre-wrap break-words">{warning}</p>
          : <div className="grid gap-1">
            <p className="whitespace-pre-wrap break-words">{explanation}</p>
            <code className="whitespace-pre-wrap break-all text-xs text-muted-foreground">{warning}</code>
          </div>}
      </li>)}
    </ul>
  </div>
}
