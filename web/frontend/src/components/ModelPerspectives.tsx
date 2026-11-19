import type { SavedModelPerspectiveView, SavedAssessmentView, MonexFiscalEPSView } from '../api/types'
import { formatJstDateTime } from '../lib/format'
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
const JUDGMENTS: Record<SavedModelPerspectiveView['assessment'] | 'context_only', { mark: string; label: string; style: string }> = {
  supportive: { mark: '＋', label: '支持', style: 'bg-emerald-500/10 text-emerald-700' },
  mixed: { mark: '±', label: '支持・相反', style: 'bg-amber-500/10 text-amber-800' },
  adverse: { mark: '−', label: '逆風', style: 'bg-rose-500/10 text-rose-700' },
  neutral: { mark: '＝', label: '中立', style: 'bg-muted text-muted-foreground' },
  context_only: { mark: 'i', label: '観測あり・個社への影響未判定', style: 'bg-sky-500/10 text-sky-800' },
  insufficient: { mark: '？', label: '情報不足', style: 'bg-muted text-muted-foreground' },
  not_applicable: { mark: '／', label: '対象外', style: 'bg-muted text-muted-foreground' },
}

const OBSERVATION_LABELS: Partial<Record<keyof typeof JUDGMENTS, string>> = {
  supportive: 'プラス側', mixed: '両方向', adverse: 'マイナス側', neutral: '方向差なし',
}
function labelFor(key: keyof typeof JUDGMENTS, estimate: boolean) {
  return (estimate ? OBSERVATION_LABELS[key] : undefined) ?? JUDGMENTS[key].label
}

function FiscalEPS({ reference }: { reference: MonexFiscalEPSView }) {
  const periods = [['今期', reference.current_fy], ['来期', reference.next_fy]] as const
  return <div className="mt-3 rounded-md bg-muted/40 p-3 text-sm">
    <p className="font-medium">アナリスト予想EPS（1株当たり利益）</p>
    <dl className="my-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
      {periods.map(([label, period]) => <div className="contents" key={label}>
        <dt>{label}{period?.fiscal_end_label && `（${period.fiscal_end_label}期）`}</dt>
        <dd>{period?.eps_jpy_per_share == null ? '未確認' : `${period.eps_jpy_per_share} 円/株`}</dd>
      </div>)}
    </dl>
    <p className="text-xs leading-6 text-muted-foreground">
      会社予想、目標株価、ローリング1年予想とは別の参考値です。
      {reference.annualized_to_12_months && '提供元が12か月換算。'}
      {reference.share_basis && '株数は提供元の最終更新時点。'}
      予想人数・公表日時は未確認。
      <br /><a className="underline" href={reference.source_url} rel="noopener noreferrer" target="_blank">配信元：マネックス証券</a>
      {' ／ 保存ページの受領：'}<time dateTime={reference.received_at_utc}>{formatJstDateTime(reference.received_at_utc)} JST</time>
    </p>
  </div>
}

export function ModelPerspectives({ assessment }: { assessment: SavedAssessmentView }) {
  // The original author always wins, including an explicit insufficient or not_applicable.
  const perspectives = assessment.model_perspectives ?? []
  const sources = assessment.model_perspective_sources ?? []
  const chart = assessment.estimated_model_chart
  const rows = MODELS.map(({ model, label }) => {
    const authored = perspectives.find(item => item.model === model)
    const estimate = !authored ? chart?.rows.find(item => item.model === model) : undefined
    return { model, label, authored, estimate, row: authored ?? estimate }
  })
  const hasEstimates = rows.some(item => item.estimate !== undefined)
  const hasAuthors = rows.some(item => item.authored !== undefined)
  return (
    <section className="grid min-w-0 gap-4" aria-label="7モデルの観点">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h4 className="text-sm font-semibold">7モデルの観点</h4>
        <p className="text-xs text-muted-foreground">評価基準日 <time dateTime={assessment.as_of}>{assessment.as_of}</time></p>
      </div>
      {hasEstimates && <p className="text-xs leading-6 text-muted-foreground">
        推定の＋・−は、黒字・赤字や騰落など観測値の方向です。その会社の投資仮説への支持・反証を意味しません。
      </p>}
      <div className="overflow-hidden rounded-xl border">
        <table className="w-full table-fixed border-collapse text-center text-xs sm:text-sm">
          <caption className="sr-only">7モデルの評価チャート。当初の判断と当時の資料からの推定を区別し、点数や賛成票にはしません。</caption>
          <thead className="bg-muted/50 text-muted-foreground">
            <tr>
              <th className="w-24 px-3 py-3 text-left font-medium sm:w-40" scope="col">観点</th>
              {CHART_COLUMNS.map(key => <th className="px-1 py-3 font-medium" key={key} scope="col">
                {hasEstimates && hasAuthors ? <><span className="block">{JUDGMENTS[key].label}</span><small className="font-normal">{OBSERVATION_LABELS[key]}</small></> : labelFor(key, hasEstimates)}
              </th>)}
            </tr>
          </thead>
          <tbody>
            {rows.map(({ model, label, row, estimate }) => {
              const judgment = row === undefined ? null : JUDGMENTS[row.assessment]
              const plotted = row !== undefined && CHART_COLUMNS.some(key => key === row.assessment)
              return <tr className="border-t" key={model} data-model={model}>
                <th className="px-3 py-3 text-left font-medium" scope="row">{label}{estimate ? <small className="block font-normal text-muted-foreground">資料からの推定</small> : hasEstimates && row && <small className="block font-normal text-muted-foreground">当初作者の評価</small>}</th>
                {plotted ? CHART_COLUMNS.map(key => <td className="px-1 py-2" key={key}>
                  {row.assessment === key ? <span className={`inline-flex size-7 items-center justify-center rounded-full text-base sm:size-8 sm:text-lg font-semibold ${JUDGMENTS[key].style}`}>
                    <span aria-hidden="true">{JUDGMENTS[key].mark}</span><span className="sr-only">{labelFor(key, !!estimate)}</span>
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
      <p className="text-xs leading-6 text-muted-foreground">当初の作者評価を優先します。「資料からの推定」は提案時の資料を後から表示用に解釈したもので、当初の作者・査読者の判断ではありません。支持・逆風は各指標に限り、売買の票ではありません。</p>
      <details className="rounded-lg border p-4">
        <summary className="cursor-pointer text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-4">7モデルの理由・根拠を読む</summary>
        <div className="mt-5 grid divide-y">
          {rows.map(({ model, label, row, authored, estimate }) => <section className="min-w-0 py-4 first:pt-0 last:pb-0" key={model}>
            <h5 className="mb-2 text-sm font-semibold">{label}<span className="ml-2 text-xs font-normal text-muted-foreground">{row ? labelFor(row.assessment, !!estimate) : '未記録'}</span></h5>
            <p className="whitespace-pre-wrap break-words text-sm leading-7">{row?.summary ?? '観点別の判断は保存されていません。'}</p>
            {model === 'external_assessment' && chart?.monex_fiscal_eps && <FiscalEPS reference={chart.monex_fiscal_eps} />}
            {authored && <><p className="mt-1 text-xs text-muted-foreground">当初の作者が記録した評価</p><AssessmentSources sourceIds={authored.source_ids} sources={sources} /></>}
            {estimate && <p className="mt-1 break-words text-xs leading-6 text-muted-foreground">固定資料からの推定。根拠：{estimate.evidence}{estimate.observed_on && <> ／ 観測日の基準：<time dateTime={estimate.observed_on}>{estimate.observed_on}</time></>}{estimate.source_url && <> ／ <a className="underline" href={estimate.source_url} target="_blank" rel="noopener noreferrer">元資料を開く</a></>}</p>}
          </section>)}
          {chart && <p className="break-all pt-4 text-xs leading-6 text-muted-foreground">
            提案入力の情報締切：<time dateTime={chart.input_known_at}>{formatJstDateTime(chart.input_known_at)} JST</time> ／ 調査基準日：{chart.research_as_of}
            <br />固定資料：{chart.selection_sha256}{chart.generation_id && <> ／ 生成：{chart.generation_id}</>}
          </p>}
          <p className="pt-4 text-xs leading-6 text-muted-foreground">機械スコアや現在の取得状況ではありません。同じ根拠を使う観点もあります。情報不足・対象外の理由と対象範囲は各説明を参照してください。</p>
        </div>
      </details>
    </section>
  )
}
