import type { AssessmentCaseView, AssessmentPurchaseView } from '../api/types'

// A bargain assessment answers one question: is there something worth buying right now.
// The three answers are equally valid conclusions, so each gets its own reading tone
// rather than treating "no proposal" as a failure state.
export const ASSESSMENT_RESULT: Record<string, { readonly label: string; readonly tone: 'positive' | 'warning' | 'muted' }> = {
  proposal: { label: '買い提案あり', tone: 'positive' },
  no_actionable_bargain: { label: '実行可能な割安なし', tone: 'muted' },
  defer: { label: '判断保留', tone: 'warning' },
}

export const ASSESSMENT_TONE_CLASS: Record<'positive' | 'warning' | 'muted', string> = {
  positive: 'bg-positive-surface text-positive-ink',
  warning: 'bg-warning-surface text-warning-ink',
  muted: 'bg-muted text-muted-foreground',
}

export const CASE_DISPOSITION: Record<string, { readonly label: string; readonly tone: 'positive' | 'warning' | 'muted' }> = {
  selected: { label: '採用', tone: 'positive' },
  reject: { label: '不採用', tone: 'muted' },
  defer: { label: '保留', tone: 'warning' },
}

// The thesis's own permanent-loss verdict, derived by the engine from the seven risk
// axes. It is the risk half of risk-reward, so the comparison shows it beside the
// return numbers rather than leaving it to the prose.
export const PERMANENT_LOSS_LABEL: Record<string, string> = {
  acceptable: '許容',
  elevated: '高い',
  unknown: '判定不能',
}

export const PERMANENT_LOSS_TONE: Record<string, 'positive' | 'warning' | 'muted'> = {
  acceptable: 'positive',
  elevated: 'warning',
  unknown: 'warning',
}

// The selected case leads so the answer reads first; the rest keep their published order
// because that is the order the assessment argues them in.
export function orderCases(cases: readonly AssessmentCaseView[]): readonly AssessmentCaseView[] {
  return [...cases]
    .map((assessmentCase, index) => ({ assessmentCase, index }))
    .sort((left, right) => {
      const leftSelected = left.assessmentCase.disposition === 'selected' ? 0 : 1
      const rightSelected = right.assessmentCase.disposition === 'selected' ? 0 : 1
      if (leftSelected !== rightSelected) return leftSelected - rightSelected
      return left.index - right.index
    })
    .map((item) => item.assessmentCase)
}

export interface PurchaseAlert {
  readonly severity: 'warning' | 'info'
  readonly message: string
}

// An assessment is an immutable judgment; the order it proposes is not. These alerts are
// the gap between the two — the reader must not act on a plan whose expiry has passed or
// whose proposal has already moved on.
export function purchaseAlerts(purchase: AssessmentPurchaseView, now: Date): readonly PurchaseAlert[] {
  const alerts: PurchaseAlert[] = []
  if (Number.isFinite(Date.parse(purchase.expires_at)) && Date.parse(purchase.expires_at) < now.getTime()) {
    alerts.push({ severity: 'warning', message: '発注期限を過ぎています。約定していなければ価格を取り直して再提案してください。' })
  }
  if (purchase.superseded) {
    alerts.push({ severity: 'warning', message: 'この proposal は application DB に見つかりません。判断文書と現状が食い違っています。' })
  } else if (purchase.current_status !== null && purchase.current_status !== 'pending') {
    alerts.push({ severity: 'info', message: `publish 後に ${purchase.current_status} へ動いています。` })
  }
  return alerts
}

// Where the limit sits against the close it was struck from, and how much room is left
// before the price stops being worth paying. Both are ratios of numbers the payload
// already carries, so no engine estimate is reproduced here.
export function limitVsClosePct(purchase: AssessmentPurchaseView): number | null {
  if (purchase.close_yen <= 0) return null
  return (purchase.limit_price_yen / purchase.close_yen - 1) * 100
}

export function headroomToMaxPct(purchase: AssessmentPurchaseView): number | null {
  if (purchase.limit_price_yen <= 0) return null
  return (purchase.max_acceptable_price_yen / purchase.limit_price_yen - 1) * 100
}
