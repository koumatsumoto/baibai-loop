import type { SavedAssessmentView } from '../api/types'
import { AssessmentSources } from './AssessmentSources'

export function AssessmentValueGap({ assessment }: { assessment: SavedAssessmentView }) {
  const gap = assessment.value_gap
  return <section className="grid min-w-0 gap-3 rounded-lg border p-4" aria-label="価値と市場評価のずれ">
    <div>
      <h4 className="text-sm font-semibold">価値と市場評価のずれ</h4>
      <p className="mt-1 text-xs leading-6 text-muted-foreground">評価基準日 <time dateTime={assessment.as_of}>{assessment.as_of}</time> の保存説明です。現在の外部予想との自動比較ではありません。</p>
    </div>
    {gap == null ? <p className="text-sm text-muted-foreground">この表示データには価値差の説明が含まれていません。</p> : <>
      <p className="whitespace-pre-wrap break-words text-sm leading-7">{gap.explanation}</p>
      <div>
        <p className="text-xs leading-6 text-muted-foreground">以下は調査所見全体の参照資料です。各資料が価値差の全記述を支持するとは限りません。</p>
        <AssessmentSources sourceIds={gap.findings_source_ids} sources={gap.findings_sources} />
      </div>
    </>}
  </section>
}
