import type { SavedModelPerspectiveView, SavedAssessmentView } from '../api/types'
import { formatJstDateTime } from '../lib/format'

const MODELS: { model: SavedModelPerspectiveView['model']; label: string }[] = [
  { model: 'company_financials', label: '企業財務' },
  { model: 'company_growth', label: '企業成長' },
  { model: 'market_context', label: '市場環境' },
  { model: 'external_assessment', label: '外部評価' },
  { model: 'technical_analysis', label: 'テクニカル分析' },
  { model: 'industry_trends', label: '産業動向' },
  { model: 'social_trends', label: '社会動向' },
]

const JUDGMENTS: Record<SavedModelPerspectiveView['assessment'], { mark: string; label: string; style: string }> = {
  supportive: { mark: '＋', label: '支持', style: 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300' },
  mixed: { mark: '±', label: '支持・相反', style: 'bg-amber-500/10 text-amber-800 dark:text-amber-300' },
  adverse: { mark: '−', label: '逆風', style: 'bg-rose-500/10 text-rose-700 dark:text-rose-300' },
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
                  {row !== undefined && row.source_ids.length > 0 && <details className="mt-2">
                    <summary className="cursor-pointer text-xs text-muted-foreground focus-visible:outline-2 focus-visible:outline-offset-4">保存根拠を確認</summary>
                    <ul className="mt-2 grid gap-3 text-xs leading-6">
                      {row.source_ids.map((id, index) => {
                        const source = sources.find(item => item.source_id === id)
                        return <li className="min-w-0 break-words rounded-md bg-muted p-3" key={`${id}-${index}`}>
                          <p className="break-all font-medium">{id}{source !== undefined && ` · ${{ primary: '一次資料', secondary: '二次資料', local_data: '保存データ' }[source.source_tier]}`}</p>
                          {source === undefined ? <p>参照先の表示情報なし</p> : <>
                            {source.ref !== null ? <a className="break-all underline" href={source.ref} target="_blank" rel="noopener noreferrer">{source.ref}</a> : <p className="break-all">保存データ：{source.provider} / {source.dataset}</p>}
                            <p>{source.used_for}</p>
                            <p>根拠の対象日：<time dateTime={source.as_of}>{source.as_of}</time></p>
                            <p>取得時刻：<time dateTime={source.retrieved_at}>{formatJstDateTime(source.retrieved_at)} JST</time></p>
                          </>}
                        </li>
                      })}
                    </ul>
                  </details>}
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
