import type { SavedModelPerspectiveView } from '../api/types'
import { PERSPECTIVE_JUDGMENTS } from '../lib/report-perspectives'

interface PerspectiveRow {
  model: SavedModelPerspectiveView['model']
  label: string
  authored?: Pick<SavedModelPerspectiveView, 'assessment'>
  observationLabel?: string
}

export function ReportPerspectiveTable({ rows, showObservations = false }: {
  rows: PerspectiveRow[]
  showObservations?: boolean
}) {
  return (
    <div className="overflow-hidden rounded-xl border">
      <table className="w-full table-fixed border-collapse text-left text-xs sm:text-sm">
        <caption className="sr-only">観点別の評価表。{showObservations ? '当時の作者判断と固定資料の観測は別の列です。' : 'この保存判断に記録された作者の評価です。'}</caption>
        <thead className="bg-muted/50 text-muted-foreground">
          <tr>
            <th className="w-[28%] px-2.5 py-3 font-medium sm:px-3" scope="col">観点</th>
            <th className="w-[33%] px-2.5 py-3 font-medium sm:px-3" scope="col">当時の作者判断</th>
            {showObservations && <th className="px-2.5 py-3 font-medium sm:px-3" scope="col">固定資料の観測</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map(({ model, label, authored, observationLabel }) => {
            const judgment = authored && PERSPECTIVE_JUDGMENTS[authored.assessment]
            return <tr className="border-t" key={model} data-model={model}>
              <th className="px-2.5 py-2.5 font-medium sm:px-3" scope="row">{label}</th>
              <td className="px-2.5 py-2.5 sm:px-3" data-perspective="author">
                {judgment ? <span className={`inline-flex items-center gap-1 rounded-md px-2 py-1 font-medium ${judgment.style}`} data-evidence="author">
                  <span aria-hidden="true">{judgment.mark}</span>{judgment.label}
                </span> : <span className="text-muted-foreground">未記録</span>}
              </td>
              {showObservations && <td className="px-2.5 py-2.5 text-muted-foreground sm:px-3" data-perspective="observation">
                {observationLabel ? <span data-evidence="observation">{observationLabel}</span>
                  : <span aria-label="観測の補足なし">—</span>}
              </td>}
            </tr>
          })}
        </tbody>
      </table>
    </div>
  )
}
