import type { AssessmentCaseView } from '../api/types'
import type { ReportTone } from '../components/report/ReportToneBadge'

// A bargain assessment answers one question: is there something worth buying right now.
// The three answers are equally valid conclusions, so each gets its own reading tone
// rather than treating "no buy" as a failure state.
export const ASSESSMENT_RESULT: Record<string, { readonly label: string; readonly tone: ReportTone }> = {
  buy: { label: '買い判断', tone: 'positive' },
  no_actionable_bargain: { label: '実行可能な割安なし', tone: 'muted' },
  defer: { label: '判断保留', tone: 'warning' },
}

// Stocks uses the same domain mapping in a compact table cell that cannot render the
// report primitive directly. Narrative report surfaces use ReportToneBadge.
export const ASSESSMENT_TONE_CLASS: Readonly<Record<ReportTone, string>> = {
  positive: 'bg-positive-surface text-positive-ink',
  warning: 'bg-warning-surface text-warning-ink',
  muted: 'bg-muted text-muted-foreground',
  destructive: 'bg-destructive-surface text-destructive-ink',
}

export const CASE_DISPOSITION: Record<string, { readonly label: string; readonly tone: ReportTone }> = {
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

export const PERMANENT_LOSS_TONE: Record<string, ReportTone> = {
  acceptable: 'positive',
  elevated: 'destructive',
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
