import type { SavedAssessmentView, SavedHypothesisCheckView } from '../api/types'
import { AssessmentSources } from './AssessmentSources'

const RESULT: Record<SavedHypothesisCheckView['result'], string> = {
  supported: '支持', refuted: '反証', unresolved: '未解決',
}
const ORIGIN: Record<SavedHypothesisCheckView['origin'], string> = {
  user: 'ユーザー', research: '調査',
}

export function AssessmentHypotheses({ assessment }: { assessment: SavedAssessmentView }) {
  const checks = assessment.hypothesis_checks
  return <details className="rounded-lg border p-4">
    <summary className="cursor-pointer text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-4">個別仮説の検討結果</summary>
    <div className="mt-4 grid min-w-0 gap-4">
      <p className="text-xs leading-6 text-muted-foreground">保存評価の作者が記録した、各命題に対する結果です。総合的な採否・Reviewerの結論・現在の事実と同一ではなく、同じ資料の引用を独立した支持票にしません。</p>
      {checks == null
        ? <p className="text-sm text-muted-foreground">この表示データには個別仮説の検討記録が含まれていません。反証や未確認事項がないことを示しません。</p>
        : checks.length === 0
          ? <p className="text-sm text-muted-foreground">この保存評価には個別仮説の記録がありません。全論点の確認済みを意味しません。</p>
          : <ol className="grid gap-4">
            {checks.map((check, index) => <li className="grid min-w-0 gap-2 rounded-md bg-muted p-3" key={index}>
              <p className="text-xs text-muted-foreground">作者記録の起点：{ORIGIN[check.origin]}（{check.origin}）</p>
              <p className="whitespace-pre-wrap break-words text-sm font-medium leading-7">{check.statement}</p>
              <p className="text-sm font-medium">{RESULT[check.result]}（{check.result}）</p>
              <p className="whitespace-pre-wrap break-words text-sm leading-7">{check.reason}</p>
              {check.source_ids.length === 0
                ? <p className="text-xs text-muted-foreground">この命題に記録された引用はありません。</p>
                : <AssessmentSources sourceIds={check.source_ids} sources={assessment.hypothesis_sources ?? []} />}
            </li>)}
          </ol>}
    </div>
  </details>
}
