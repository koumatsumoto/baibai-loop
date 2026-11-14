import { reportExcerpt } from '../lib/report-excerpt'

export function ReportSummary({ text }: { text: string }) {
  const excerpt = reportExcerpt(text)
  return <div className="grid gap-2" data-report-summary>
    <p className="whitespace-pre-wrap break-words text-base leading-8">{excerpt}</p>
    {excerpt !== text && <p className="text-xs text-muted-foreground">保存本文の冒頭3文。全文は下の詳細で読めます。</p>}
  </div>
}
