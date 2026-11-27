import type { EstimatedPerspectiveView, MonexFiscalEPSView, SavedAssessmentView } from '../api/types'
import { formatJstDateTime } from '../lib/format'
import { AssessmentSources } from './AssessmentSources'
import { PERSPECTIVES_TITLE, PERSPECTIVE_JUDGMENTS, REPORT_MODELS } from '../lib/report-perspectives'
import { ReportPerspectiveTable } from './ReportPerspectiveTable'

// The retained report encodes directions with some of the same wire values, not the same meanings.
const OBSERVATIONS: Record<EstimatedPerspectiveView['assessment'], string> = {
  supportive: 'プラス側',
  mixed: '両方向',
  adverse: 'マイナス側',
  neutral: '方向差なし',
  context_only: '観測あり・個社への影響未判定',
  insufficient: '情報不足',
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
  const perspectives = assessment.model_perspectives ?? []
  const sources = assessment.model_perspective_sources ?? []
  const fixed = assessment.estimated_model_chart
  const rows = REPORT_MODELS.map(({ model, label }) => {
    const authored = perspectives.find(item => item.model === model)
    // A recorded insufficient/not_applicable is still the author's judgment.
    const observation = authored ? undefined : fixed?.rows.find(item => item.model === model)
    return { model, label, authored, observation, observationLabel: observation && OBSERVATIONS[observation.assessment] }
  })
  const hasObservation = rows.some(({ observation }) => observation !== undefined)
  return (
    <section className="grid min-w-0 gap-4" aria-label={PERSPECTIVES_TITLE}>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h4 className="text-sm font-semibold">{PERSPECTIVES_TITLE}</h4>
        <p className="text-xs text-muted-foreground">評価基準日 <time dateTime={assessment.as_of}>{assessment.as_of}</time></p>
      </div>
      {hasObservation && (
        <p className="text-xs leading-6 text-muted-foreground">
          資料のプラス・マイナスは黒字・赤字や騰落などの方向です。投資仮説への支持・反証を意味しません。
        </p>
      )}
      <ReportPerspectiveTable rows={rows} showObservations />
      {hasObservation && (
        <p className="text-xs leading-6 text-muted-foreground">提案時の固定資料を、後から表示用に読み取った観測です。作者・査読者の判断ではなく、点数や売買の票にしません。</p>
      )}
      <details className="rounded-lg border p-4">
        <summary className="cursor-pointer text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-4">観点別の理由・根拠を読む</summary>
        <div className="mt-5 grid divide-y">
          {rows.map(({ model, label, authored, observation }) => <section className="min-w-0 py-4 first:pt-0 last:pb-0" key={model}>
            <h5 className="mb-2 text-sm font-semibold">{label}<span className="ml-2 text-xs font-normal text-muted-foreground">{
              authored ? `作者：${PERSPECTIVE_JUDGMENTS[authored.assessment].label}`
                : observation ? `観測：${OBSERVATIONS[observation.assessment]}` : '未記録'
            }</span></h5>
            <p className="whitespace-pre-wrap break-words text-sm leading-7">{authored?.summary ?? observation?.summary ?? '観点別の判断は保存されていません。'}</p>
            {model === 'external_assessment' && fixed?.monex_fiscal_eps && <FiscalEPS reference={fixed.monex_fiscal_eps} />}
            {authored && <AssessmentSources sourceIds={authored.source_ids} sources={sources} />}
            {observation && <p className="mt-1 break-words text-xs leading-6 text-muted-foreground">
              固定資料の根拠：{observation.evidence}{observation.observed_on && <> ／ 観測日の基準：<time dateTime={observation.observed_on}>{observation.observed_on}</time></>}{observation.source_url && <> ／ <a className="underline" href={observation.source_url} target="_blank" rel="noopener noreferrer">元資料を開く</a></>}
            </p>}
          </section>)}
          {fixed && <p className="break-all pt-4 text-xs leading-6 text-muted-foreground">
            提案入力の情報締切：<time dateTime={fixed.input_known_at}>{formatJstDateTime(fixed.input_known_at)} JST</time> ／ 調査基準日：{fixed.research_as_of}
            <br />固定資料：{fixed.selection_sha256}{fixed.generation_id && <> ／ 生成：{fixed.generation_id}</>}
          </p>}
        </div>
      </details>
    </section>
  )
}
