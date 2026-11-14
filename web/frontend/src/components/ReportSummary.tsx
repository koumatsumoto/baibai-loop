// Saved prose is not guaranteed to put its conclusion or key caveat in the first sentences.
export function ReportSummary({ text }: { text: string }) {
  return <div className="grid gap-2" data-report-summary>
    <p className="whitespace-pre-wrap break-words text-base leading-8">{text}</p>
  </div>
}
