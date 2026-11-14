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

// Categories, not an ordinal scale: mixed is not halfway between adverse and supportive.
const CHART_COLUMNS = ['supportive', 'mixed', 'adverse', 'neutral'] as const
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
  const rows = MODELS.map(({ model, label }) => ({ model, label, row: perspectives.find(item => item.model === model) }))
  return (
    <section className="grid min-w-0 gap-4" aria-label="7モデルの観点">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h4 className="text-sm font-semibold">7モデルの観点</h4>
        <p className="text-xs text-muted-foreground">評価基準日 <time dateTime={assessment.as_of}>{assessment.as_of}</time></p>
      </div>
      <div className="overflow-hidden rounded-xl border">
        <table className="w-full table-fixed border-collapse text-center text-xs sm:text-sm">
          <caption className="sr-only">7モデルの評価チャート。保存判断の分類であり、点数や賛成票ではありません。</caption>
          <thead className="bg-muted/50 text-muted-foreground">
            <tr>
              <th className="w-24 px-3 py-3 text-left font-medium sm:w-40" scope="col">観点</th>
              {CHART_COLUMNS.map(key => <th className="px-1 py-3 font-medium" key={key} scope="col">{JUDGMENTS[key].label}</th>)}
            </tr>
          </thead>
          <tbody>
            {rows.map(({ model, label, row }) => {
              const judgment = row === undefined ? null : JUDGMENTS[row.assessment]
              const plotted = row !== undefined && CHART_COLUMNS.some(key => key === row.assessment)
              return <tr className="border-t" key={model} data-model={model}>
                <th className="px-3 py-3 text-left font-medium" scope="row">{label}</th>
                {plotted ? CHART_COLUMNS.map(key => <td className="px-1 py-2" key={key}>
                  {row.assessment === key ? <span className={`inline-flex size-7 items-center justify-center rounded-full text-base sm:size-8 sm:text-lg font-semibold ${JUDGMENTS[key].style}`}>
                    <span aria-hidden="true">{JUDGMENTS[key].mark}</span><span className="sr-only">{JUDGMENTS[key].label}</span>
                  </span> : <span aria-hidden="true" className="text-muted-foreground/25">·</span>}
                </td>) : <td className="px-2 py-3" colSpan={4}>
                  <span className={`inline-flex items-center gap-1 rounded-md px-3 py-1 text-xs ${judgment?.style ?? 'border border-dashed text-muted-foreground'}`}>
                    <span aria-hidden="true">{judgment?.mark ?? '…'}</span>{judgment?.label ?? '未記録'}
                  </span>
                </td>}
              </tr>
            })}
          </tbody>
        </table>
      </div>
      <p className="text-xs leading-6 text-muted-foreground">保存された判断を一覧化しています。点数・多数決ではなく、未記録は中立と区別します。</p>
      <details className="rounded-lg border p-4">
        <summary className="cursor-pointer text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-4">7モデルの理由・根拠を読む</summary>
        <div className="mt-5 grid divide-y">
          {rows.map(({ model, label, row }) => <section className="min-w-0 py-4 first:pt-0 last:pb-0" key={model}>
            <h5 className="mb-2 text-sm font-semibold">{label}<span className="ml-2 text-xs font-normal text-muted-foreground">{row ? JUDGMENTS[row.assessment].label : '未記録'}</span></h5>
            <p className="whitespace-pre-wrap break-words text-sm leading-7">{row?.summary ?? '観点別の判断は保存されていません。'}</p>
            {row !== undefined && <AssessmentSources sourceIds={row.source_ids} sources={sources} />}
          </section>)}
          <p className="pt-4 text-xs leading-6 text-muted-foreground">機械スコアや現在の取得状況ではありません。同じ根拠を使う観点もあります。情報不足・対象外の理由と対象範囲は各説明を参照してください。</p>
        </div>
      </details>
    </section>
  )
}
