import type { SavedModelPerspectiveView, SavedAssessmentView } from '../api/types'
import { AssessmentSources } from './AssessmentSources'

const MODELS: { model: SavedModelPerspectiveView['model']; label: string }[] = [
  { model: 'company_financials', label: '企業財務' },
  { model: 'company_growth', label: '企業成長' },
  { model: 'market_context', label: '市場環境' },
  { model: 'external_assessment', label: '外部評価' },
  { model: 'technical_analysis', label: 'テクニカル分析' },
  { model: 'industry_trends', label: '産業動向' },
  { model: 'social_trends', label: '社会動向' },
]

// アプリの背景はライト固定なので、OS設定だけで文字を淡色にしない。
const JUDGMENTS: Record<SavedModelPerspectiveView['assessment'], { mark: string; label: string; style: string }> = {
  supportive: { mark: '＋', label: '支持', style: 'bg-emerald-500/10 text-emerald-700' },
  mixed: { mark: '±', label: '支持・相反', style: 'bg-amber-500/10 text-amber-800' },
  adverse: { mark: '−', label: '逆風', style: 'bg-rose-500/10 text-rose-700' },
  neutral: { mark: '＝', label: '中立', style: 'bg-muted text-muted-foreground' },
  insufficient: { mark: '？', label: '情報不足', style: 'bg-muted text-muted-foreground' },
  not_applicable: { mark: '／', label: '対象外', style: 'bg-muted text-muted-foreground' },
}

export function ModelPerspectives({ assessment }: { assessment: SavedAssessmentView }) {
  // Older serving artifacts have no section. Never infer it from stance or model availability.
  const perspectives = assessment.model_perspectives ?? []
  const sources = assessment.model_perspective_sources ?? []
  return (
    <section className="grid min-w-0 gap-3 rounded-lg border p-4" aria-label="7モデルの観点">
      <div className="grid gap-1">
        <h4 className="text-sm font-semibold">7モデルの観点</h4>
        <p className="text-xs leading-6 text-muted-foreground">評価基準日 <time dateTime={assessment.as_of}>{assessment.as_of}</time> の保存判断です。機械スコアや現在の取得状況ではありません。同じ根拠を使う観点もあり、記号は独立した賛成票・点数ではありません。</p>
      </div>
      <table className="w-full table-fixed border-collapse text-left text-sm">
        <caption className="sr-only">モデル別の保存判断と、企業価値・価格・条件への意味</caption>
        <thead>
          <tr className="border-b text-xs text-muted-foreground">
            <th className="w-28 pb-3 pr-3 font-medium sm:w-40" scope="col">観点 / 判断</th>
            <th className="pb-3 font-medium" scope="col">企業価値・価格・条件への意味</th>
          </tr>
        </thead>
        <tbody>
          {MODELS.map(({ model, label }) => {
            const row = perspectives.find(item => item.model === model)
            const judgment = row === undefined ? null : JUDGMENTS[row.assessment]
            return (
              <tr className="border-b last:border-0" key={model} data-model={model}>
                <th className="py-4 pr-3 align-top font-medium" scope="row">
                  <span className="block">{label}</span>
                  <span className={`mt-2 inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs ${judgment?.style ?? 'border border-dashed text-muted-foreground'}`}>
                    <span aria-hidden="true">{judgment?.mark ?? '…'}</span>{judgment?.label ?? '未記録'}
                  </span>
                </th>
                <td className="min-w-0 py-4 align-top">
                  <p className="whitespace-pre-wrap break-words leading-7">{row?.summary ?? '観点別の判断は保存されていません。'}</p>
                  {row !== undefined && <AssessmentSources sourceIds={row.source_ids} sources={sources} />}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
      <p className="text-xs leading-6 text-muted-foreground">未記録は中立・0点・情報不足と同じ意味ではありません。情報不足や対象外の理由、対象期間・範囲は各行の説明を確認してください。</p>
    </section>
  )
}
