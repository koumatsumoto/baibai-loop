import type { USProposalPerspectiveView } from '../api/types'
import { formatJstDateTime } from '../lib/format'
import { PERSPECTIVES_TITLE, REPORT_MODELS } from '../lib/report-perspectives'
import { ReportPerspectiveTable } from './ReportPerspectiveTable'

export function USReportPerspectives({ perspectives }: { perspectives: USProposalPerspectiveView[] | null }) {
  const rows = REPORT_MODELS.map(({ model, label }) => ({
    model, label, authored: perspectives?.find(row => row.model === model),
  }))
  return <section aria-label={PERSPECTIVES_TITLE} className="grid min-w-0 gap-3 border-t pt-4">
    <h4 className="font-medium">{PERSPECTIVES_TITLE}</h4>
    {perspectives === null
      ? <p className="text-muted-foreground">この配信データからは観点別の評価を読み取れません。評価が未記録という意味ではありません。</p>
      : perspectives.length === 0
      ? <p className="text-muted-foreground">この判断には観点別の評価が保存されていません。株価や判断の結論から推定していません。</p>
      : <>
        <ReportPerspectiveTable rows={rows} />
        <details className="rounded-lg border p-4">
          <summary className="cursor-pointer font-medium focus-visible:outline-2 focus-visible:outline-offset-4">観点別の理由・根拠を読む</summary>
          <div className="mt-3 grid divide-y">
            {rows.map(({ model, label, authored }) => authored && <section key={model} className="grid min-w-0 gap-2 py-3">
              <h5 className="font-medium">{label}</h5>
              <p className="whitespace-pre-wrap break-words">{authored.summary}</p>
              {authored.sources.length > 0 && <ul className="grid gap-1 text-xs leading-6 text-muted-foreground">
                {authored.sources.map(({ label: source, observed_at }, index) => <li key={index} className="break-words">
                  {source} ／ 資料の確認日時：<time dateTime={observed_at}>{formatJstDateTime(observed_at)} JST</time>
                </li>)}
              </ul>}
            </section>)}
          </div>
        </details>
      </>}
  </section>
}
